const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow, ipcMain } = require("electron");


const frontendUrl = process.env.AEGIS_CAPTURE_URL || "http://127.0.0.1:5173";
const apiBaseUrl = process.env.AEGIS_CAPTURE_API || "http://127.0.0.1:8002";
const uploadKnowledgeBaseName = process.env.AEGIS_CAPTURE_KB_NAME || "";
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
      additionalArguments: [`--aegis-api-base=${apiBaseUrl}`, "--aegis-version=2.1.0"],
    },
  });
  await window.loadURL(frontendUrl);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('.example-grid'))"));
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('.sidebar-status button small')?.textContent.match(/[1-9]/))"));
  await saveCapture(window, "empty-1440x920.png");
  await window.webContents.executeJavaScript("document.querySelectorAll('.example-grid button')[1].click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('.answer-card'))"));
  await new Promise((resolve) => setTimeout(resolve, 400));
  window.setSize(1080, 720);
  await new Promise((resolve) => setTimeout(resolve, 250));
  await saveCapture(window, "answer-1080x720.png");
  window.setSize(1440, 920);
  await window.webContents.executeJavaScript("document.querySelectorAll('.sidebar-status button')[0].click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('.knowledge-page'))"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('.index-facts span')?.textContent.includes('300')"));
  await saveCapture(window, "knowledge-1440x920.png");

  if (uploadKnowledgeBaseName) {
    const selected = await window.webContents.executeJavaScript(`(() => {
      const target = ${JSON.stringify(uploadKnowledgeBaseName)};
      const button = [...document.querySelectorAll('.library-panel nav button')]
        .find((item) => item.querySelector('strong')?.textContent === target);
      button?.click();
      return Boolean(button);
    })()`, true);
    if (!selected) throw new Error(`Knowledge base was not found: ${uploadKnowledgeBaseName}`);
    await waitFor(() => window.webContents.executeJavaScript(
      `document.querySelector('.documents-head h2')?.textContent === ${JSON.stringify(uploadKnowledgeBaseName)}`,
    ));
    await waitFor(() => window.webContents.executeJavaScript(
      "document.querySelectorAll('.document-row').length >= 4 && [...document.querySelectorAll('.document-status')].every((item) => item.textContent.includes('片段'))",
    ));
    await saveCapture(window, "knowledge-upload-1440x920.png");

    await window.webContents.executeJavaScript("document.querySelector('.document-row button')?.click()", true);
    await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('.document-preview'))"));
    await window.webContents.executeJavaScript("document.querySelector('.modal-card header .icon-button')?.click()", true);
    await waitFor(() => window.webContents.executeJavaScript("!document.querySelector('.modal-layer')"));

    await window.webContents.executeJavaScript("document.querySelector('.document-row > div:last-child button:last-child')?.click()", true);
    await waitFor(() => window.webContents.executeJavaScript("document.querySelector('.modal-card h3')?.textContent === '删除文档'"));
    await window.webContents.executeJavaScript("document.querySelector('.modal-card footer .secondary-button')?.click()", true);
    await waitFor(() => window.webContents.executeJavaScript("!document.querySelector('.modal-layer')"));

    await window.webContents.executeJavaScript("document.querySelector('.document-actions button')?.click()", true);
    await waitFor(() => window.webContents.executeJavaScript(
      `document.querySelector('.knowledge-select-wrap select option:checked')?.textContent === ${JSON.stringify(uploadKnowledgeBaseName)} && Boolean(document.querySelector('.empty-state'))`,
    ));
    await window.webContents.executeJavaScript(`(() => {
      const select = document.querySelector('.knowledge-select-wrap select');
      select.value = 'developer-it';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    })()`, true);
    await waitFor(() => window.webContents.executeJavaScript(
      "document.querySelector('.knowledge-select-wrap select')?.value === 'developer-it' && Boolean(document.querySelector('.empty-state'))",
    ));
  } else {
    await window.webContents.executeJavaScript("document.querySelector('.back-button').click()", true);
    await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('.chat-panel'))"));
  }
  window.showInactive();
  await window.webContents.executeJavaScript("document.querySelectorAll('.sidebar-status button')[1].click()", true);
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('.settings-drawer')?.classList.contains('open')"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('.settings-drawer')?.getBoundingClientRect().right <= window.innerWidth"));
  await new Promise((resolve) => setTimeout(resolve, 350));
  await saveCapture(window, "settings-1440x920.png");
  window.hide();
  window.destroy();
  app.quit();
}).catch((error) => {
  console.error(error.message);
  app.exit(1);
});
