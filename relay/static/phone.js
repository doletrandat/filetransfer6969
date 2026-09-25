const phoneToken = document.querySelector('meta[name="relay-phone-token"]').content;
const elements = {
  files: document.querySelector("#phoneFiles"),
  selection: document.querySelector("#selection"),
  uploadButton: document.querySelector("#uploadButton"),
  progress: document.querySelector("#uploadProgress"),
  uploadStatus: document.querySelector("#uploadStatus"),
  uploadedList: document.querySelector("#uploadedList"),
  refreshButton: document.querySelector("#refreshButton"),
  downloadList: document.querySelector("#downloadList"),
  connectionStatus: document.querySelector("#connectionStatus"),
};

function formatBytes(value) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  if (!value) return "0 B";
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function fileDetails(name, size) {
  const details = document.createElement("div");
  details.className = "file-details";
  const title = document.createElement("strong");
  title.textContent = name;
  const subtitle = document.createElement("span");
  subtitle.textContent = formatBytes(size);
  details.append(title, subtitle);
  return details;
}

function renderDownloads(items) {
  elements.downloadList.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("li");
    empty.textContent = "Chưa có tệp. Hãy chọn tệp trên máy tính trước.";
    elements.downloadList.append(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement("li");
    const link = document.createElement("a");
    link.className = "download-link";
    link.href = `/phone/api/files/${encodeURIComponent(item.id)}`;
    link.download = item.name.split(/[\\/]/).at(-1);
    link.textContent = "Tải về";
    row.append(fileDetails(item.name, item.size), link);
    elements.downloadList.append(row);
  }
}

function renderUploads(items) {
  elements.uploadedList.replaceChildren();
  for (const item of items) {
    const row = document.createElement("li");
    const details = fileDetails(item.name, item.size);
    details.querySelector("span").textContent += ` · Đã lưu trong ${item.folder}`;
    row.append(details);
    elements.uploadedList.append(row);
  }
}

async function refresh() {
  try {
    const response = await fetch("/phone/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(response.status === 401
      ? "Kết nối đã hết hạn. Hãy tạo mã QR mới trên máy tính."
      : "Không kết nối được với Relay. Hãy kiểm tra Wi-Fi.");
    const data = await response.json();
    renderDownloads(data.available);
    renderUploads(data.uploaded);
    elements.connectionStatus.textContent = "Đã kết nối với máy tính qua mạng nội bộ.";
    elements.connectionStatus.classList.remove("error");
  } catch (error) {
    elements.connectionStatus.textContent = error.message;
    elements.connectionStatus.classList.add("error");
  }
}

function uploadFile(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `/phone/api/files?filename=${encodeURIComponent(file.name)}`);
    request.setRequestHeader("X-Relay-Phone-Token", phoneToken);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        elements.progress.value = Math.round((event.loaded / event.total) * 100);
      }
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) {
        resolve(JSON.parse(request.responseText));
      } else {
        let message = "Gửi tệp không thành công.";
        try { message = JSON.parse(request.responseText).detail || message; } catch (_) { /* Keep default. */ }
        reject(new Error(message));
      }
    };
    request.onerror = () => reject(new Error("Mất kết nối trong lúc gửi tệp."));
    request.send(file);
  });
}

elements.files.addEventListener("change", () => {
  const count = elements.files.files.length;
  elements.selection.textContent = count ? `Đã chọn ${count} tệp.` : "Chưa chọn tệp.";
  elements.uploadButton.disabled = !count;
});

elements.uploadButton.addEventListener("click", async () => {
  const files = [...elements.files.files];
  if (!files.length) return;
  elements.uploadButton.disabled = true;
  elements.progress.hidden = false;
  elements.uploadStatus.classList.remove("error");
  let sent = 0;
  let lastFolder = "";
  try {
    for (const file of files) {
      elements.progress.value = 0;
      elements.uploadStatus.textContent = `Đang gửi ${file.name} (${sent + 1}/${files.length})…`;
      const result = await uploadFile(file);
      lastFolder = result.folder;
      sent += 1;
      elements.uploadStatus.textContent = `Đã gửi ${file.name}. Máy tính đã lưu trong ${lastFolder}.`;
      await refresh();
    }
    elements.uploadStatus.textContent = `Đã gửi ${sent} tệp. Máy tính đã lưu trong ${lastFolder}.`;
    elements.files.value = "";
    elements.selection.textContent = "Chưa chọn tệp.";
  } catch (error) {
    elements.uploadStatus.textContent = `Đã gửi ${sent} tệp. ${error.message}`;
    elements.uploadStatus.classList.add("error");
  } finally {
    elements.progress.hidden = true;
    elements.uploadButton.disabled = !elements.files.files.length;
  }
});

elements.refreshButton.addEventListener("click", refresh);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
refresh();
