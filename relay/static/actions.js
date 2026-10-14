import { state, elements } from "./state.js";
import { api, inferBatchName, showToast, selectedPeers, peerAlreadySending, notifyReceived } from "./ui.js";
import { renderAll, renderStaged, renderTarget, renderSidebar } from "./render.js";

let refreshPromise = null;

async function refresh() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const payload = await api("/api/v1/state");
    state.connectionError = false;
    const hadSnapshot = state.hasSnapshot;
    const previousStatuses = new Map([
      ...state.outgoing.map((transfer) => [`sent:${transfer.id}`, transfer.status]),
      ...state.incoming.map((transfer) => [`received:${transfer.id}`, transfer.status]),
    ]);
    state.status = payload.status;
    state.devices = payload.devices;
    state.receivingPeers = payload.receiving_peers || [];
    state.staged = payload.staged;
    state.outgoing = payload.outgoing;
    state.incoming = payload.incoming;
    state.incomingRequests = payload.incoming_requests || [];
    const previousPhoneStatus = state.phoneConnection?.status;
    state.phoneConnection = payload.phone_connection;
    const previousIds = new Set(state.phoneUploads.map((item) => item.id));
    const newUploads = (payload.phone_uploads || []).filter((item) =>
      (!item.status || item.status === "complete") && !previousIds.has(item.id));
    state.phoneUploads = payload.phone_uploads || [];
    for (const transfer of [...state.outgoing.map((item) => ({ ...item, key: `sent:${item.id}` })),
      ...state.incoming.map((item) => ({ ...item, key: `received:${item.id}` }))]) {
      if (hadSnapshot && transfer.status === "complete" && previousStatuses.has(transfer.key)
        && previousStatuses.get(transfer.key) !== "complete") {
        state.completedAt.set(transfer.key, Date.now());
        if (transfer.key.startsWith("received:")) notifyReceived(`Đã nhận ${transfer.batch_name} từ ${transfer.source.name}.`);
      }
    }
    state.selectedPeerIds = new Set([...state.selectedPeerIds].filter((id) => state.devices.some((peer) => peer.id === id)));
    state.hasSnapshot = true;
    renderAll();
    if (hadSnapshot && previousPhoneStatus !== "connected" && state.phoneConnection?.status === "connected") {
      showToast("Điện thoại đã kết nối với Relay.");
    }
    if (hadSnapshot && newUploads.length) {
      const message = newUploads.length === 1 ? `Đã nhận ${newUploads[0].name} từ điện thoại` : `Đã nhận ${newUploads.length} tệp từ điện thoại`;
      notifyReceived(message);
    }
  })().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
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
        renderSidebar();
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
  const peers = selectedPeers().filter((peer) => !peerAlreadySending(peer));
  if (!peers.length || !state.staged.length || elements.sendButton.disabled || state.sending) return;
  const itemIds = state.staged.map((item) => item.id);
  const batchName = inferBatchName();
  state.sending = true;
  state.pendingPeerName = peers.map((peer) => peer.name).join(", ");
  renderAll();
  elements.sendProgress.scrollIntoView({ block: "start", behavior: "smooth" });
  try {
    const results = await Promise.allSettled(peers.map(async (peer) => {
      const transfer = await api("/api/v1/transfers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          peer_id: peer.id,
          batch_name: batchName,
          item_ids: itemIds,
          retain_staged: true,
        }),
      });
      state.outgoing = [transfer, ...state.outgoing.filter((item) => item.id !== transfer.id)];
      renderAll();
    }));
    const failures = results.flatMap((result, index) => result.status === "rejected"
      ? [`${peers[index].name}: ${result.reason.message}`] : []);
    // Keep only unsuccessful targets selected so retrying cannot resend to successful peers.
    for (let index = 0; index < results.length; index++) {
      if (failures.length && results[index].status === "fulfilled") state.selectedPeerIds.delete(peers[index].id);
    }
    showToast(failures.length ? `Đã bắt đầu ${peers.length - failures.length}/${peers.length} lượt gửi. ${failures.join("; ")}`
      : `Đã gửi yêu cầu đến ${peers.length} thiết bị. Đang chờ đồng ý.`, Boolean(failures.length));
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.sending = false;
    renderAll();
  }
}

export {
  refresh,
  connectPhone,
  disconnectPhone,
  uploadFiles,
  send
};
