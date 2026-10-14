import { state, elements } from "./state.js";
import {
  escapeHtml, formatBytes, formatSpeed, percent, fileName, showToast,
  selectedPeers, peerAlreadySending, isSending, formatDuration,
} from "./ui.js";

const lastRenderedHtml = new WeakMap();

function renderStatus() {
  if (!state.status) return;
  const { device, addresses, destination } = state.status;
  elements.deviceName.textContent = device.name;
  const lanAddress = addresses.find((address) => !address.startsWith("127."));
  elements.deviceAddress.textContent = lanAddress || "127.0.0.1";
  elements.fingerprint.textContent = device.fingerprint;
  if (document.activeElement !== elements.destinationInput) elements.destinationInput.value = destination;
  const hasDevices = state.devices.length > 0;
  elements.discoveryText.textContent = hasDevices ? `${state.devices.length} máy gần đây` : "Đang tìm";
  elements.discoveryLamp.style.background = hasDevices ? "var(--green)" : "var(--blue)";

  // Trạng thái kết nối chung ở góc trên phải
  if (elements.systemStatusPill) elements.systemStatusPill.classList.remove("is-offline");
  if (elements.systemStatusText) elements.systemStatusText.textContent = "Trực tuyến";

  // Thông tin mạng trong chi tiết kết nối của thiết bị
  if (elements.deviceNetwork) {
    elements.deviceNetwork.textContent = lanAddress ? "Mạng cục bộ (LAN)" : "Chỉ cục bộ (Loopback)";
    elements.deviceNetwork.classList.toggle("is-warning", !lanAddress);
  }

  // Trạng thái nhận tệp tích hợp vào dòng mô tả dưới tiêu đề "Nhận tệp"
  if (elements.receiveSubtitle) {
    if (lanAddress) {
      elements.receiveSubtitle.textContent = "Sẵn sàng nhận tệp từ các máy tính và điện thoại trong cùng mạng.";
      elements.receiveSubtitle.classList.remove("is-warning", "is-error");
    } else {
      elements.receiveSubtitle.textContent = "Chưa kết nối Wi-Fi hoặc mạng LAN. Cần kết nối mạng để nhận tệp từ thiết bị khác.";
      elements.receiveSubtitle.classList.add("is-warning");
      elements.receiveSubtitle.classList.remove("is-error");
    }
  }
}

function renderPhoneConnection() {
  const status = state.phoneConnection?.status;
  elements.phoneConnectionStatus.textContent = status === "connected"
    ? "Điện thoại đã kết nối. Có thể gửi hoặc nhận tệp."
    : status === "waiting"
      ? "Đang chờ điện thoại quét mã QR."
      : "Chưa có điện thoại kết nối.";
  elements.phoneConnectionStatus.classList.toggle("is-connected", status === "connected");
  elements.phoneDisconnectButton.hidden = status !== "connected" && status !== "waiting";
  elements.phoneDisconnectButton.textContent = status === "waiting" ? "Hủy mã QR" : "Ngắt kết nối";
}

function renderDevices() {
  if (!state.hasSnapshot) return;
  const devices = [...state.devices];
  if (!devices.length) {
    elements.deviceList.innerHTML = `
      <p class="empty-message">Chưa thấy máy tính nào. Hãy mở Relay trên máy còn lại và kiểm tra cả hai dùng chung mạng.</p>`;
    return;
  }
  setHtmlIfChanged(elements.deviceList, devices.map((device) => {
    const selected = state.selectedPeerIds.has(device.id);
    return `
      <div class="device-row${selected ? " selected" : ""}">
        <label class="device-select">
          <input type="checkbox" data-device-id="${escapeHtml(device.id)}" aria-label="Chọn ${escapeHtml(device.name)}" ${selected ? "checked" : ""}>
          <span>
            <span class="device-name">${escapeHtml(device.name)}</span>
            <span class="device-address">${escapeHtml(device.host)}:${device.port} · ${escapeHtml(device.fingerprint.slice(0, 11))}</span>
          </span>
          <span class="device-state">Sẵn sàng</span>
        </label>
      </div>`;
  }).join(""));
}

function renderTarget() {
  const peers = selectedPeers();
  const targets = peers.length === 1 ? peers[0].name : `${peers.length} thiết bị`;
  elements.targetStamp.textContent = peers.length ? targets : "Chưa chọn";
  elements.targetStamp.title = peers.map((peer) => peer.name).join(", ");
  elements.transferSubtitle.textContent = peers.length
    ? `${targets} sẽ được hỏi trước khi nhận tệp. Tệp đã chọn cũng hiện trên điện thoại đã kết nối để tải về.`
    : "Chọn máy tính trong danh sách để gửi yêu cầu nhận tệp. Nếu gửi cho điện thoại, tệp được chọn sẽ hiện trên điện thoại để tải về.";
  const available = peers.filter((peer) => !peerAlreadySending(peer));
  const ready = Boolean(available.length && state.staged.length && !state.uploading && !state.sending);
  elements.sendButton.disabled = !ready;
  elements.sendButton.querySelector("span").textContent = state.sending ? "Đang chuẩn bị gửi…"
    : peers.length && !available.length ? "Đang gửi đến các máy đã chọn"
      : available.length === 1 ? `Gửi đến ${available[0].name}`
        : available.length ? `Gửi đến ${available.length} thiết bị` : "Chọn máy tính nhận";
}

function renderSendProgress() {
  const ordered = [...state.outgoing].sort((a, b) => b.created_at - a.created_at);
  const active = ordered.filter(isSending);
  const transfers = [...active, ...ordered.filter((transfer) => !isSending(transfer)).slice(0, 5)];
  elements.sendProgress.hidden = !transfers.length && !state.sending;
  elements.sendProgressSummary.textContent = active.length
    ? `${active.length} lượt đang gửi · Cập nhật tự động`
    : "Kết quả các lượt gửi gần đây";
  elements.sendPending.hidden = !state.sending;
  elements.sendPending.textContent = state.sending
    ? `Đang chờ ${state.pendingPeerName} chuẩn bị nhận tệp…` : "";
  elements.sendConnectionWarning.hidden = !state.connectionError;
  elements.sendConnectionWarning.textContent = "Mất kết nối với Relay. Tiến trình bên dưới là lần cập nhật gần nhất; đang thử kết nối lại…";
  setHtmlIfChanged(elements.sendProgressList, transfers.map((transfer) => {
    const current = state.staged.find((item) => item.id === transfer.current_item_id);
    const complete = transfer.status === "complete";
    const failed = transfer.status === "failed";
    const cancelled = transfer.status === "cancelled";
    const label = complete ? "Hoàn tất" : cancelled ? "Đã hủy" : failed ? "Gửi thất bại"
      : transfer.status === "rejected" ? "Đã từ chối"
      : transfer.status === "expired" ? "Hết thời gian chờ"
      : transfer.status === "waiting" ? "Chờ máy nhận đồng ý"
        : transfer.status === "preparing" ? "Đang chuẩn bị" : "Đang gửi";
    const fileCount = transfer.item_ids.length;
    const completedCount = complete ? fileCount : transfer.completed_item_ids.length;
    const fileDetail = current ? `${failed || cancelled ? "Tệp bị gián đoạn" : "Tệp hiện tại"}: ${current.relative_path}`
      : complete ? "Máy nhận đã xác nhận đầy đủ tệp."
        : cancelled ? "Lượt gửi đã dừng theo yêu cầu."
        : failed ? "Kiểm tra kết nối với máy nhận rồi thử gửi lại." : "Đang chờ máy nhận xác nhận…";
    return `<article class="send-transfer" data-send-transfer="${escapeHtml(transfer.id)}">
      <div class="send-transfer-heading">
        <div class="row-main"><strong class="send-transfer-name">${escapeHtml(transfer.batch_name)}</strong>
          <span class="send-transfer-peer">Đến ${escapeHtml(transfer.peer_name)} · ${fileCount} tệp</span></div>
        <span class="transfer-state ${escapeHtml(transfer.status)}" role="status">${label}</span>
      </div>
      <div class="send-transfer-detail"><span>${escapeHtml(fileDetail)}</span><strong data-send-field="percent"></strong></div>
      <div class="transfer-track" role="progressbar" aria-label="Tiến trình gửi ${escapeHtml(transfer.batch_name)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="transfer-fill"></div></div>
      <dl class="send-metrics">
        <div><dt>Đã gửi / Tổng</dt><dd data-send-field="bytes"></dd></div>
        <div><dt>Tốc độ</dt><dd data-send-field="speed"></dd></div>
        <div><dt>Còn lại ước tính</dt><dd data-send-field="eta"></dd></div>
        <div><dt>Thời gian đã chạy</dt><dd data-send-field="elapsed"></dd></div>
      </dl>
      <div class="send-transfer-footer"><span>${completedCount}/${fileCount} tệp hoàn tất</span>
        ${isSending(transfer) ? `<button class="cancel-button" type="button" data-cancel-transfer="${escapeHtml(transfer.id)}" data-cancel-direction="outgoing" ${state.cancelling.has(transfer.id) ? "disabled" : ""}>${state.cancelling.has(transfer.id) ? "Đang hủy…" : "Hủy gửi"}</button>` : failed ? `<button class="retry-button" type="button" data-retry-transfer="${escapeHtml(transfer.id)}" ${state.retrying.has(transfer.id) ? "disabled" : ""}>${state.retrying.has(transfer.id) ? "Đang thử lại…" : "Thử gửi lại"}</button>` : ""}</div>
      ${transfer.error && !cancelled ? `<p class="transfer-error">${escapeHtml(transfer.error)}</p>` : ""}
    </article>`;
  }).join(""));
  const now = Date.now() / 1000;
  for (const card of elements.sendProgressList.querySelectorAll("[data-send-transfer]")) {
    const transfer = transfers.find((item) => item.id === card.dataset.sendTransfer);
    const complete = transfer.status === "complete";
    const activeTransfer = isSending(transfer);
    const progress = complete ? 100 : Math.min(99, percent(transfer.sent_bytes, transfer.total_bytes));
    const recent = !state.connectionError && now - transfer.updated_at < 10;
    const speed = activeTransfer && recent ? transfer.speed_bps : 0;
    const remaining = Math.max(0, transfer.total_bytes - transfer.sent_bytes);
    const values = {
      percent: `${progress}%`,
      bytes: `${formatBytes(transfer.sent_bytes)} / ${formatBytes(transfer.total_bytes)}`,
      speed: activeTransfer ? speed > 0 ? formatSpeed(speed) : "Đang chờ dữ liệu…" : "—",
      eta: complete ? "Hoàn tất" : !activeTransfer ? "—"
        : !remaining ? "Chờ xác nhận" : speed > 0 ? `Khoảng ${formatDuration(remaining / speed)}` : "Đang tính…",
      elapsed: formatDuration((activeTransfer ? now : transfer.updated_at) - transfer.created_at),
    };
    for (const [name, value] of Object.entries(values)) card.querySelector(`[data-send-field="${name}"]`).textContent = value;
    const track = card.querySelector('[role="progressbar"]');
    track.setAttribute("aria-valuenow", String(progress));
    track.querySelector(".transfer-fill").style.transform = `scaleX(${progress / 100})`;
  }
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
  const busyItems = new Set(state.outgoing.filter(isSending).flatMap((transfer) => transfer.item_ids));
  elements.clearStagedButton.disabled = !state.staged.length || state.uploading || state.sending || busyItems.size > 0;
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
      <button class="file-card-remove" type="button" data-remove-item="${escapeHtml(item.id)}" aria-label="Bỏ ${escapeHtml(fileName(item.relative_path))}" ${state.sending || busyItems.has(item.id) ? 'disabled title="Tệp đang được gửi"' : ""}>
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4l12 12M16 4L4 16"></path></svg>
      </button>
    </div>`).join("");
}

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
    name: item.name, counterpart: item.device_name || "Điện thoại", size: item.size,
    status: item.status || "complete", transferred: item.received_bytes ?? item.size, speed: 0,
    error: item.error || "", phoneItem: item.status ? null : item,
    phoneTransfer: item.status ? item : null, items: [],
  }));
  const requests = state.incomingRequests.filter((offer) => ["rejected", "expired", "cancelled"].includes(offer.status)).map((offer) => ({
    kind: "received", id: `request:${offer.id}`, time: offer.created_at,
    name: offer.batch_name, counterpart: offer.source.name, size: offer.total_bytes,
    status: offer.status, transferred: 0, speed: 0, error: "", items: [],
  }));
  return [...outgoing, ...incoming, ...phone, ...requests].sort((a, b) => b.time - a.time);
}

function entryStatus(entry) {
  const label = entry.status === "complete" ? "Hoàn tất"
    : entry.status === "failed" ? "Lỗi"
      : entry.status === "cancelled" ? "Đã hủy"
      : entry.status === "rejected" ? "Đã từ chối"
      : entry.status === "expired" ? "Hết thời gian chờ"
      : entry.status === "waiting" ? (entry.kind === "sent" ? "Chờ máy nhận đồng ý" : "Chờ gửi lại")
        : entry.kind === "sent" ? "Đang gửi" : "Đang nhận";
  const recentlyCompleted = Date.now() - (state.completedAt.get(entry.id) || 0) < 1200;
  return `<span class="transfer-state ${escapeHtml(entry.status)}${recentlyCompleted ? " new-complete" : ""}">${entry.status === "complete" ? '<span class="check" aria-hidden="true">✓</span>' : ""}${label}</span>`;
}

function entryActions(entry, includeCopy = false) {
  if (entry.phoneTransfer && entry.status === "receiving") {
    const cancelling = state.cancelling.has(`phone:${entry.phoneTransfer.id}`);
    return `<button class="cancel-button" type="button" data-cancel-phone-upload="${escapeHtml(entry.phoneTransfer.id)}" ${cancelling ? "disabled" : ""}>${cancelling ? "Đang hủy…" : "Hủy nhận"}</button>`;
  }
  if (entry.phoneItem) {
    const item = entry.phoneItem;
    return item.available
      ? `<button class="row-action" type="button" data-open-phone-upload="${escapeHtml(item.id)}">Mở tệp</button>${includeCopy ? `<button class="row-action" type="button" data-copy-phone-upload="${escapeHtml(item.id)}">Sao chép đường dẫn</button>` : ""}`
      : '<span class="row-meta">Tệp không còn trên máy</span>';
  }
  if (entry.kind === "sent") {
    if (isSending(entry)) {
      const cancelling = state.cancelling.has(entry.transferId);
      return `<button class="cancel-button" type="button" data-cancel-transfer="${escapeHtml(entry.transferId)}" data-cancel-direction="outgoing" ${cancelling ? "disabled" : ""}>${cancelling ? "Đang hủy…" : "Hủy gửi"}</button>`;
    }
    return entry.status === "failed"
      ? `<button class="retry-button" type="button" data-retry-transfer="${escapeHtml(entry.transferId)}">Thử gửi lại</button>`
      : "";
  }
  if (["receiving", "waiting"].includes(entry.status)) {
    const cancelling = state.cancelling.has(entry.transferId);
    return `<button class="cancel-button" type="button" data-cancel-transfer="${escapeHtml(entry.transferId)}" data-cancel-direction="incoming" ${cancelling ? "disabled" : ""}>${cancelling ? "Đang hủy…" : "Hủy nhận"}</button>`;
  }
  const completed = entry.items.filter((item) => item.completed);
  return completed.map((item) => item.available
    ? `<button class="received-file-link" type="button" data-open-computer-transfer="${escapeHtml(entry.transferId)}" data-open-computer-item="${escapeHtml(item.id)}" title="Mở ${escapeHtml(item.relative_path)} bằng ứng dụng trên máy">${completed.length === 1 ? "Mở tệp" : escapeHtml(item.name)}</button>`
    : `<span class="row-meta">${escapeHtml(item.name)} · Tệp không còn trên máy</span>`).join("");
}

function entryProgress(entry) {
  if (["complete", "failed", "cancelled", "rejected", "expired"].includes(entry.status)) return "";
  return `<div class="transfer-track" role="progressbar" data-progress-entry="${escapeHtml(entry.id)}" aria-label="${escapeHtml(entry.name)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="transfer-fill" style="transform: scaleX(0)"></div></div>`;
}

function syncActiveProgress(container, entries) {
  const active = new Map(entries.filter((entry) => !["complete", "failed", "cancelled", "rejected", "expired"].includes(entry.status))
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
  const detail = ["complete", "failed", "cancelled", "rejected", "expired"].includes(entry.status) ? `Từ ${escapeHtml(entry.counterpart)}`
    : `Từ ${escapeHtml(entry.counterpart)} · <span data-progress-meta="${escapeHtml(entry.id)}"></span>`;
  return `<div class="receive-row">
    <span class="row-direction received" aria-hidden="true">↓</span>
    <div class="row-main"><strong class="row-name" title="${escapeHtml(entry.name)}">${escapeHtml(entry.name)}</strong><span class="row-meta">${detail}</span></div>
    <span class="row-size">${formatBytes(entry.size)}</span><time class="row-time" datetime="${new Date(entry.time * 1000).toISOString()}">${entryTime(entry)}</time>
    <div class="row-actions">${entryStatus(entry)}${actions}</div>
    ${entryProgress(entry)}${entry.error && entry.status !== "cancelled" ? `<p class="transfer-error">${escapeHtml(entry.error)}</p>` : ""}
  </div>`;
}

function historyRow(entry) {
  const actions = entryActions(entry);
  const completedItems = entry.items.filter((item) => item.completed);
  const showActionsBelow = completedItems.length > 1 || completedItems.some((item) => !item.available)
    || (entry.phoneItem && !entry.phoneItem.available);
  const detail = `${entry.kind === "sent" ? "Đến" : "Từ"} ${escapeHtml(entry.counterpart)}${["complete", "failed", "cancelled", "rejected", "expired"].includes(entry.status) ? "" : ` · <span data-progress-meta="${escapeHtml(entry.id)}"></span>`}`;
  return `<div class="history-row">
    <span class="row-direction ${entry.kind}" aria-hidden="true">${entry.kind === "sent" ? "↑" : "↓"}</span>
    <div class="row-main"><strong class="row-name" title="${escapeHtml(entry.name)}">${escapeHtml(entry.name)}</strong><span class="row-meta">${detail}</span></div>
    <span class="row-size">${formatBytes(entry.size)}</span><time class="row-time" datetime="${new Date(entry.time * 1000).toISOString()}">${entryTime(entry)}</time>
    ${entryStatus(entry)}<div class="row-actions">${showActionsBelow ? "" : actions}</div>
    ${showActionsBelow ? `<div class="row-extra">${actions}</div>` : ""}
    ${entryProgress(entry)}${entry.error && entry.status !== "cancelled" ? `<p class="transfer-error">${escapeHtml(entry.error)}</p>` : ""}
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

function renderSidebar() {
  const phones = state.phoneConnection?.status === "connected"
    ? state.phoneConnection.devices || [{ name: "Điện thoại" }] : [];
  const computers = [...new Map([...state.devices, ...state.receivingPeers].map((peer) => [peer.id, peer])).values()];
  const devices = [...computers, ...phones];
  setHtmlIfChanged(elements.connectedDevicesList, state.connectionError
    ? '<p class="sidebar-empty">Mất kết nối với Relay</p>'
    : devices.length ? devices.map((device) => `<div class="sidebar-device"><span class="device-lamp" aria-hidden="true"></span><span>${escapeHtml(device.name)}</span></div>`).join("")
      : '<p class="sidebar-empty">Chưa có thiết bị</p>');
  const active = transferEntries().filter((entry) => ["preparing", "sending", "receiving", "waiting"].includes(entry.status));
  const rows = active.map((entry) => {
    const progress = Math.min(99, percent(entry.transferred, entry.size));
    const label = state.connectionError ? "Chờ kết nối" : entry.status === "rejected" ? "Đã từ chối"
      : entry.status === "expired" ? "Hết thời gian chờ"
      : entry.status === "waiting" ? (entry.kind === "sent" ? "Chờ máy nhận đồng ý" : "Chờ gửi lại") : entry.kind === "sent" ? "Đang gửi" : "Đang nhận";
    return `<button type="button" class="sidebar-transfer" data-sidebar-view="${entry.kind === "sent" ? "send" : "receive"}">
      <strong>${escapeHtml(entry.counterpart)}</strong><span>${escapeHtml(entry.name)}</span>
      <span class="sidebar-transfer-status">${label} · ${progress}%</span>
      <progress max="100" value="${progress}" aria-label="${escapeHtml(label + " " + entry.name)}"></progress></button>`;
  });
  if (state.uploading) rows.unshift(`<p class="sidebar-empty">Đang chuẩn bị tệp · ${percent(state.uploadProgress?.loaded || 0, state.uploadProgress?.total)}%</p>`);
  if (state.sending) rows.unshift(`<p class="sidebar-empty">Đang kết nối đến ${escapeHtml(state.pendingPeerName)}…</p>`);
  setHtmlIfChanged(elements.sidebarProgressList, rows.join("") || '<p class="sidebar-empty">Không có lượt truyền</p>');
}

function renderPhoneUploads() {
  const entries = transferEntries().filter((entry) => entry.kind === "received");
  elements.phoneUploadCount.textContent = `${entries.length} lượt`;
  setHtmlIfChanged(elements.phoneUploadsList, entries.length
    ? entries.map(receiveRow).join("")
    : '<p class="empty-message">Chưa nhận tệp nào. Kết nối điện thoại hoặc máy tính khác để bắt đầu.</p>');
  syncActiveProgress(elements.phoneUploadsList, entries);
}

function renderAll() {
  renderStatus();
  renderPhoneConnection();
  renderDevices();
  renderTarget();
  renderStaged();
  renderTransfers();
  renderSendProgress();
  renderPhoneUploads();
  renderRequests();
  renderSidebar();
}

function renderRequests() {
  const offers = state.incomingRequests.filter((offer) => offer.status === "waiting");
  setHtmlIfChanged(elements.requestList, offers.map((offer) => `
    <article class="receive-request">
      <h3>${escapeHtml(offer.source.name)} muốn gửi ${offer.file_count} tệp, tổng ${formatBytes(offer.total_bytes)}</h3>
      <p class="row-meta">Chỉ nhận nếu bạn nhận ra máy gửi. Quyền nhận chỉ áp dụng cho lượt này.</p>
      <ul class="request-files">${offer.items.slice(0, 8).map((item) => `<li>${escapeHtml(item.relative_path)} <span>${formatBytes(item.size)}</span></li>`).join("")}</ul>
      ${offer.items.length > 8 ? `<p>Và ${offer.items.length - 8} tệp khác</p>` : ""}
      <div class="request-actions">
        <button class="button secondary" type="button" data-request-id="${escapeHtml(offer.id)}" data-decision="reject" ${state.decidingRequests.has(offer.id) ? "disabled" : ""}>Từ chối</button>
        <button class="button primary" type="button" data-request-id="${escapeHtml(offer.id)}" data-decision="accept" ${state.decidingRequests.has(offer.id) ? "disabled" : ""}>Nhận</button>
      </div>
    </article>`).join(""));
  if (offers.length && !elements.requestDialog.open) elements.requestDialog.showModal();
  if (!offers.length && elements.requestDialog.open) elements.requestDialog.close();
}

export {
  renderStatus,
  renderPhoneConnection,
  renderDevices,
  renderTarget,
  renderSendProgress,
  renderStaged,
  renderTransfers,
  renderSidebar,
  renderPhoneUploads,
  renderAll,
  renderRequests
};
