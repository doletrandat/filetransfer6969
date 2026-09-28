const connectForm = document.querySelector('form[action="/phone/connect"]');
const modelInput = document.querySelector("#phoneDeviceModel");

const detectedModel = (async () => {
  try {
    const hints = await navigator.userAgentData?.getHighEntropyValues(["model"]);
    return typeof hints?.model === "string" ? hints.model.slice(0, 80) : "";
  } catch (_) {
    return "";
  }
})();

let connecting = false;
connectForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (connecting) return;
  connecting = true;
  connectForm.querySelector('button[type="submit"]').disabled = true;
  // A blocked or slow hints API must not prevent pairing; the server also checks the UA.
  modelInput.value = await Promise.race([
    detectedModel,
    new Promise((resolve) => window.setTimeout(() => resolve(""), 800)),
  ]);
  HTMLFormElement.prototype.submit.call(connectForm);
});
