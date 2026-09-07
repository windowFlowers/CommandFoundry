const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow, ipcMain } = require("electron");


const frontendUrl = process.env.AEGIS_CAPTURE_URL || "http://127.0.0.1:5173";
const apiBaseUrl = process.env.AEGIS_CAPTURE_API || "http://127.0.0.1:8002";
const outputRoot = path.resolve(__dirname, "..", "..", "docs", "screenshots");

function waitFor(getter, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (await getter()) return resolve();
      if (Date.now() >= deadline) return reject(new Error("UI capture timed out"));
      setTimeout(tick, 150);
    };
    tick();
  });
}

async function saveCapture(window, filename) {
  const image = await window.webContents.capturePage();
  fs.writeFileSync(path.join(outputRoot, filename), image.toPNG());
}

app.whenReady().then(async () => {
  ipcMain.handle("model-config:status", () => ({ configured: false }));
  fs.mkdirSync(outputRoot, { recursive: true });
  const window = new BrowserWindow({
    width: 1440,
    height: 920,
    show: false,
    backgroundColor: "#edf1f3",
    titleBarStyle: "hidden",
    titleBarOverlay: { color: "#dbe2e6", symbolColor: "#34434d", height: 38 },
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.resolve(__dirname, "..", "electron", "preload.cjs"),
      additionalArguments: [`--aegis-api-base=${apiBaseUrl}`, "--aegis-version=2.0.0"],
    },
  });
  await window.loadURL(frontendUrl);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('.example-grid'))"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('.knowledge-row strong')?.textContent.includes('100')"));
  await saveCapture(window, "empty-1440x920.png");
  await window.webContents.executeJavaScript("document.querySelectorAll('.example-grid button')[1].click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('.answer-card'))"));
  await new Promise((resolve) => setTimeout(resolve, 400));
  window.setSize(1080, 720);
  await new Promise((resolve) => setTimeout(resolve, 250));
  await saveCapture(window, "answer-1080x720.png");
  window.destroy();
  app.quit();
}).catch((error) => {
  console.error(error.message);
  app.exit(1);
});
