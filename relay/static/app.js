import { state, elements } from "./state.js";
import { api, showToast, showView, askNotificationsOnce } from "./ui.js";
import {
  renderAll, renderStaged, renderTarget, renderSendProgress,
  renderTransfers, renderSidebar, renderRequests,
} from "./render.js";
import { refresh, connectPhone, disconnectPhone, uploadFiles, send } from "./actions.js";

elements.deviceList.addEventListener("change", (event) => {
  const checkbox = event.target.closest("[data-device-id]");
  if (!checkbox) return;
  if (checkbox.checked) state.selectedPeerIds.add(checkbox.dataset.deviceId);
  else state.selectedPeerIds.delete(checkbox.dataset.deviceId);
  renderAll();
});
elements.sidebarProgressList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-sidebar-view]");
  if (button) showView(button.dataset.sidebarView);
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

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-retry-transfer]");
  if (!button || state.retrying.has(button.dataset.retryTransfer)) return;
  const transferId = button.dataset.retryTransfer;
  state.retrying.add(transferId);
  button.disabled = true;
  renderSendProgress();
  try {
    await api(`/api/v1/transfers/${encodeURIComponent(transferId)}/retry`, { method: "POST", timeoutMs: 120000 });
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.retrying.delete(transferId);
    button.disabled = false;
    renderSendProgress();
  }
});

document.addEventListener("click", async (event) => {
  const transferButton = event.target.closest("[data-cancel-transfer]");
  const phoneButton = event.target.closest("[data-cancel-phone-upload]");
  if (!transferButton && !phoneButton) return;
  const key = phoneButton
    ? `phone:${phoneButton.dataset.cancelPhoneUpload}`
    : transferButton.dataset.cancelTransfer;
  if (state.cancelling.has(key)) return;
  state.cancelling.add(key);
  renderAll();
  try {
    const path = phoneButton
      ? `/api/v1/phone/uploads/${encodeURIComponent(phoneButton.dataset.cancelPhoneUpload)}`
      : `/api/v1/transfers/${encodeURIComponent(transferButton.dataset.cancelDirection)}/${encodeURIComponent(transferButton.dataset.cancelTransfer)}`;
    await api(path, { method: "DELETE", timeoutMs: 15000 });
    await refresh();
    showToast(phoneButton || transferButton.dataset.cancelDirection === "incoming"
      ? "Đã hủy nhận tệp." : "Đã hủy gửi tệp.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.cancelling.delete(key);
    renderAll();
  }
});

elements.viewSendHistoryButton.addEventListener("click", () => showView("activity"));

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

elements.phoneConnectButton.addEventListener("click", connectPhone);
elements.phoneDisconnectButton.addEventListener("click", disconnectPhone);
function closeDialog(backdrop) {
  if (backdrop.hidden) return;
  backdrop.hidden = true;
  elements.phoneConnectButton.focus();
}
elements.closePhoneInviteButton.addEventListener("click", () => closeDialog(elements.phoneInvite));
for (const backdrop of [elements.phoneInvite]) {
  backdrop.addEventListener("click", (event) => {
    if (event.target !== backdrop) return;
    closeDialog(backdrop);
  });
}
document.addEventListener("keydown", (event) => {
  const backdrop = !elements.phoneInvite.hidden ? elements.phoneInvite : null;
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
  elements.notificationDialog.close();
  try { await Notification.requestPermission(); } catch (_) { /* In-page notices remain available. */ }
});
elements.dismissNotificationsButton.addEventListener("click", () => elements.notificationDialog.close());

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
    state.connectionError = true;
    renderSendProgress();
    renderSidebar();
    showToast(error.message, true);
    if (elements.systemStatusPill) elements.systemStatusPill.classList.add("is-offline");
    if (elements.systemStatusText) elements.systemStatusText.textContent = "Mất kết nối";
    if (elements.receiveSubtitle) {
      elements.receiveSubtitle.textContent = "Không thể kết nối đến máy chủ Relay. Vui lòng kiểm tra ứng dụng Relay trên máy tính.";
      elements.receiveSubtitle.classList.add("is-error");
      elements.receiveSubtitle.classList.remove("is-warning");
    }
  }
  window.clearTimeout(refreshTimer);
  const hasActiveTransfer = state.outgoing.some((transfer) => ["preparing", "sending", "waiting"].includes(transfer.status))
    || state.incoming.some((transfer) => ["receiving", "waiting"].includes(transfer.status))
    || state.phoneUploads.some((transfer) => transfer.status === "receiving");
  const delay = hasActiveTransfer ? 500 : document.hidden ? 5000 : 2000;
  refreshTimer = window.setTimeout(pollState, delay);
}

document.addEventListener("visibilitychange", () => {
  window.clearTimeout(refreshTimer);
  pollState();
});
window.addEventListener("online", pollState);
window.addEventListener("offline", () => {
  if (elements.systemStatusPill) elements.systemStatusPill.classList.add("is-offline");
  if (elements.systemStatusText) elements.systemStatusText.textContent = "Ngoại tuyến";
  if (elements.receiveSubtitle) {
    elements.receiveSubtitle.textContent = "Thiết bị đang ngoại tuyến. Vui lòng kết nối mạng Wi-Fi hoặc mạng LAN để nhận tệp.";
    elements.receiveSubtitle.classList.add("is-warning");
    elements.receiveSubtitle.classList.remove("is-error");
  }
});
askNotificationsOnce();
showView("receive");
pollState();

elements.requestDialog.addEventListener("cancel", (event) => event.preventDefault());
elements.requestList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-request-id]");
  if (!button || state.decidingRequests.has(button.dataset.requestId)) return;
  const id = button.dataset.requestId;
  state.decidingRequests.add(id);
  renderRequests();
  try {
    await api(`/api/v1/requests/${encodeURIComponent(id)}/${button.dataset.decision}`, { method: "POST" });
    await refresh();
    if (button.dataset.decision === "accept") showView("receive");
    showToast(button.dataset.decision === "accept" ? "Đã đồng ý nhận tệp." : "Đã từ chối lượt gửi.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.decidingRequests.delete(id);
    renderRequests();
  }
});
