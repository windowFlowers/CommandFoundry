const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow, ipcMain } = require("electron");


const frontendUrl = process.env.AEGIS_CAPTURE_URL || "http://127.0.0.1:5173";
const apiBaseUrl = process.env.AEGIS_CAPTURE_API || "http://127.0.0.1:8002";
const uploadKnowledgeBaseName = process.env.AEGIS_CAPTURE_KB_NAME || "";
const outputRoot = path.resolve(__dirname, "..", "..", "docs", "screenshots");
let captureConversationId = null;
let captureKnowledgeBaseId = null;

async function cleanupCaptureConversation() {
  if (!captureConversationId) return;
  const response = await fetch(`${apiBaseUrl}/conversations/${encodeURIComponent(captureConversationId)}`, { method: "DELETE" });
  if (!response.ok && response.status !== 404) throw new Error(`Could not clean up capture conversation (${response.status})`);
  captureConversationId = null;
}

async function cleanupCaptureKnowledgeBase() {
  if (!captureKnowledgeBaseId) return;
  const response = await fetch(`${apiBaseUrl}/knowledge-bases/${encodeURIComponent(captureKnowledgeBaseId)}`, { method: "DELETE" });
  if (!response.ok && response.status !== 404) throw new Error(`Could not clean up capture knowledge base (${response.status})`);
  captureKnowledgeBaseId = null;
}

async function cleanupCaptureArtifacts() {
  const results = await Promise.allSettled([
    cleanupCaptureConversation(),
    cleanupCaptureKnowledgeBase(),
  ]);
  const failures = results.filter((result) => result.status === "rejected");
  if (failures.length) throw new Error(failures.map((result) => result.reason?.message || String(result.reason)).join("; "));
}

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
  await window.webContents.executeJavaScript("new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  const image = await window.webContents.capturePage();
  fs.writeFileSync(path.join(outputRoot, filename), image.toPNG());
}

async function assertNoHorizontalOverflow(window, label) {
  const metrics = await window.webContents.executeJavaScript(`(() => ({
    viewport: document.documentElement.clientWidth,
    documentWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
  }))()`);
  if (metrics.documentWidth > metrics.viewport + 1 || metrics.bodyWidth > metrics.viewport + 1) {
    throw new Error(`${label} has horizontal overflow: ${JSON.stringify(metrics)}`);
  }
}

app.whenReady().then(async () => {
  ipcMain.handle("model-config:status", () => ({ configured: false }));
  fs.mkdirSync(outputRoot, { recursive: true });
  const window = new BrowserWindow({
    width: 1440,
    height: 920,
    show: false,
    backgroundColor: "#efeee9",
    titleBarStyle: "hidden",
    titleBarOverlay: { color: "#e5e2da", symbolColor: "#232626", height: 38 },
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
      preload: path.resolve(__dirname, "..", "electron", "preload.cjs"),
      additionalArguments: [`--aegis-api-base=${apiBaseUrl}`, "--aegis-version=2.3.0"],
    },
  });
  await window.loadURL(frontendUrl);
  window.showInactive();
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"example-grid\"]'))"));
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"open-knowledge\"] small')?.textContent.match(/[1-9]/))"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"knowledge-select\"] option:checked')?.textContent.includes('开发者 IT') && document.querySelector('.knowledge-status-label')?.textContent === '已就绪'"));
  await new Promise((resolve) => setTimeout(resolve, 350));
  const initialKnowledgeBasesResponse = await fetch(`${apiBaseUrl}/knowledge-bases`);
  if (!initialKnowledgeBasesResponse.ok) throw new Error(`Could not verify initial knowledge bases (${initialKnowledgeBasesResponse.status})`);
  const initialKnowledgeBaseCount = ((await initialKnowledgeBasesResponse.json()).items || []).length;
  await waitFor(() => window.webContents.executeJavaScript(`document.querySelector('[data-ui="open-knowledge"] small')?.textContent === ${JSON.stringify(`${initialKnowledgeBaseCount} 个知识库`)} && document.querySelector('.knowledge-status-label')?.textContent === '已就绪'`));
  await window.webContents.executeJavaScript("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }))", true);
  await waitFor(() => window.webContents.executeJavaScript("document.activeElement?.id === 'chat-query'"));
  await window.webContents.executeJavaScript("document.activeElement?.blur()", true);
  await new Promise((resolve) => setTimeout(resolve, 250));
  await assertNoHorizontalOverflow(window, "empty-1440x920");
  await saveCapture(window, "empty-1440x920.png");
  await window.webContents.executeJavaScript("document.querySelectorAll('[data-ui=\"example-grid\"] button')[1].click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"answer-card\"]'))"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('.chat-panel > [role=\"status\"]')?.textContent.includes('回答已生成')"));
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"answer-card\"] .command-card h3'))"));
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('.session-item.active[data-conversation-id]'))"));
  captureConversationId = await window.webContents.executeJavaScript("document.querySelector('.session-item.active')?.dataset.conversationId", true);
  if (!captureConversationId) throw new Error("Capture conversation id was not exposed by the UI");
  await new Promise((resolve) => setTimeout(resolve, 400));
  window.setSize(1080, 720);
  await new Promise((resolve) => setTimeout(resolve, 250));
  await assertNoHorizontalOverflow(window, "answer-1080x720");
  await saveCapture(window, "answer-1080x720.png");
  window.setSize(1440, 920);
  await window.webContents.executeJavaScript(`(() => {
    const textarea = document.querySelector('[data-ui="composer"] textarea');
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(textarea, '这个方法会丢代码吗？');
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  })()`, true);
  await waitFor(() => window.webContents.executeJavaScript("!document.querySelector('[data-ui=\"composer\"] > button')?.disabled"));
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"composer\"] textarea').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true }))", true);
  await waitFor(() => window.webContents.executeJavaScript("document.querySelectorAll('[data-ui=\"answer-card\"]').length >= 2"));
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"context-badge\"]'))"));
  window.showInactive();
  await window.webContents.executeJavaScript("(() => { const trigger = [...document.querySelectorAll('[data-ui=\"context-badge\"]')].at(-1); trigger.focus(); trigger.click(); })()", true);
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"memory-drawer\"]')?.getAttribute('aria-hidden') === 'false'"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"memory-drawer\"]')?.getBoundingClientRect().right <= window.innerWidth"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"memory-drawer\"]')?.contains(document.activeElement)"));
  await new Promise((resolve) => setTimeout(resolve, 250));
  await saveCapture(window, "memory-1440x920.png");
  window.setSize(1080, 720);
  await new Promise((resolve) => setTimeout(resolve, 250));
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"clear-memory\"]').click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"memory-reset-modal\"]'))"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"memory-reset-modal\"]')?.contains(document.activeElement)"));
  const nestedOverlayIsolated = await window.webContents.executeJavaScript(`(() => {
    const drawer = document.querySelector('[data-ui="memory-drawer"]');
    const workspace = document.querySelector('.workspace');
    const modal = document.querySelector('[data-ui="memory-reset-modal"]');
    document.querySelector('[data-ui="new-chat"]')?.focus();
    return drawer?.getAttribute('aria-hidden') === 'true'
      && drawer?.hasAttribute('inert')
      && workspace?.getAttribute('aria-hidden') === 'true'
      && workspace?.hasAttribute('inert')
      && modal?.contains(document.activeElement);
  })()`, true);
  if (!nestedOverlayIsolated) throw new Error("Nested overlay isolation validation failed");
  const modalLoopsFocus = await window.webContents.executeJavaScript(`(() => {
    const modal = document.querySelector('[data-ui="memory-reset-modal"]');
    const controls = [...modal.querySelectorAll('button:not([disabled])')];
    controls[0].focus();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }));
    const wrappedBackward = document.activeElement === controls.at(-1);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
    return wrappedBackward && document.activeElement === controls[0];
  })()`, true);
  if (!modalLoopsFocus) throw new Error("Modal focus loop validation failed");
  await new Promise((resolve) => setTimeout(resolve, 200));
  await saveCapture(window, "memory-reset-1080x720.png");
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"memory-reset-modal\"] footer .secondary-button').click()", true);
  await waitFor(() => window.webContents.executeJavaScript("!document.querySelector('[data-ui=\"memory-reset-modal\"]')"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"memory-drawer\"]')?.getAttribute('aria-hidden') === 'false' && !document.querySelector('[data-ui=\"memory-drawer\"]')?.hasAttribute('inert')"));
  await window.webContents.executeJavaScript("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))", true);
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"memory-drawer\"]')?.getAttribute('aria-hidden') === 'true'"));
  await waitFor(() => window.webContents.executeJavaScript("document.activeElement?.matches('[data-ui=\"context-badge\"]')"));
  window.setSize(1440, 920);
  await new Promise((resolve) => setTimeout(resolve, 200));
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"open-knowledge\"]').click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"knowledge-page\"]'))"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('.index-facts span')?.textContent.includes('300')"));
  await window.webContents.executeJavaScript("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }))", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"chat-panel\"]')) && document.activeElement?.id === 'chat-query'"));
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"open-knowledge\"]').click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"knowledge-page\"]'))"));

  const captureKnowledgeBaseName = "视觉验收知识库";
  await window.webContents.executeJavaScript(`(() => {
    const input = document.querySelector('#new-library-name');
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(input, ${JSON.stringify(captureKnowledgeBaseName)});
    input.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector('[data-ui="new-library-form"]').requestSubmit();
  })()`, true);
  await waitFor(() => window.webContents.executeJavaScript(
    `document.querySelector('.documents-head h2')?.textContent === ${JSON.stringify(captureKnowledgeBaseName)}`,
  ));
  const captureKnowledgeBasesResponse = await fetch(`${apiBaseUrl}/knowledge-bases`);
  if (!captureKnowledgeBasesResponse.ok) throw new Error(`Could not resolve capture knowledge base (${captureKnowledgeBasesResponse.status})`);
  const captureKnowledgeBases = (await captureKnowledgeBasesResponse.json()).items || [];
  captureKnowledgeBaseId = captureKnowledgeBases.find((base) => !base.is_builtin && base.name === captureKnowledgeBaseName)?.id || null;
  if (!captureKnowledgeBaseId) throw new Error("Capture knowledge base id was not returned by the API");
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"document-table\"] .empty-documents'))"));
  await assertNoHorizontalOverflow(window, "knowledge-empty-1440x920");
  await saveCapture(window, "knowledge-empty-1440x920.png");

  await window.webContents.executeJavaScript(`(() => {
    const content = '# Command Safety Notes\\n\\nUse pwd before changing files. Prefer dry-run modes and verify every destructive target.';
    const transfer = new DataTransfer();
    transfer.items.add(new File([content], 'command-safety-notes.md', { type: 'text/markdown' }));
    const input = document.querySelector('.file-input');
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  })()`, true);
  await waitFor(() => window.webContents.executeJavaScript(
    "Boolean(document.querySelector('[data-ui=\"document-row\"]')) && [...document.querySelectorAll('.document-status')].every((item) => item.textContent.includes('子块'))",
  ));
  await assertNoHorizontalOverflow(window, "knowledge-content-1440x920");
  await saveCapture(window, "knowledge-1440x920.png");
  await saveCapture(window, "knowledge-content-1440x920.png");
  await saveCapture(window, "knowledge-upload-1440x920.png");

  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"preview-document\"]:not([disabled])').click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"document-preview-modal\"]'))"));
  await saveCapture(window, "document-preview-1440x920.png");
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"document-preview-modal\"] header .icon-button').click()", true);
  await waitFor(() => window.webContents.executeJavaScript("!document.querySelector('[data-ui=\"document-preview-modal\"]')"));

  window.setSize(1080, 720);
  await new Promise((resolve) => setTimeout(resolve, 200));
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"delete-document\"]').click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"delete-confirm-modal\"]'))"));
  await new Promise((resolve) => setTimeout(resolve, 200));
  await assertNoHorizontalOverflow(window, "delete-confirm-1080x720");
  await saveCapture(window, "delete-confirm-1080x720.png");
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"delete-confirm-modal\"] footer .secondary-button').click()", true);
  await waitFor(() => window.webContents.executeJavaScript("!document.querySelector('[data-ui=\"delete-confirm-modal\"]')"));
  window.setSize(1440, 920);
  await new Promise((resolve) => setTimeout(resolve, 200));

  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"delete-knowledge-base\"]').click()", true);
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"delete-confirm-modal\"]'))"));
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"delete-confirm-modal\"] footer .danger-button').click()", true);
  await waitFor(() => window.webContents.executeJavaScript(
    `document.querySelector('.documents-head h2')?.textContent !== ${JSON.stringify(captureKnowledgeBaseName)} && !document.querySelector('[data-ui="delete-confirm-modal"]')`,
  ));
  captureKnowledgeBaseId = null;
  await waitFor(() => window.webContents.executeJavaScript("document.activeElement?.matches('[data-ui=\"upload-zone\"]')"));

  if (uploadKnowledgeBaseName) {
    const selected = await window.webContents.executeJavaScript(`(() => {
      const target = ${JSON.stringify(uploadKnowledgeBaseName)};
      const button = [...document.querySelectorAll('[data-ui="knowledge-base-list"] button')]
        .find((item) => item.querySelector('strong')?.textContent === target);
      button?.click();
      return Boolean(button);
    })()`, true);
    if (!selected) throw new Error(`Knowledge base was not found: ${uploadKnowledgeBaseName}`);
    await waitFor(() => window.webContents.executeJavaScript(
      `document.querySelector('.documents-head h2')?.textContent === ${JSON.stringify(uploadKnowledgeBaseName)}`,
    ));
    await waitFor(() => window.webContents.executeJavaScript(
      "document.querySelectorAll('[data-ui=\"document-row\"]').length >= 4 && [...document.querySelectorAll('.document-status')].every((item) => item.textContent.includes('子块'))",
    ));
    await saveCapture(window, "knowledge-upload-1440x920.png");

    await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"preview-document\"]:not([disabled])')?.click()", true);
    await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"document-preview-modal\"]'))"));
    await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"document-preview-modal\"] header .icon-button')?.click()", true);
    await waitFor(() => window.webContents.executeJavaScript("!document.querySelector('[data-ui=\"document-preview-modal\"]')"));

    await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"delete-document\"]')?.click()", true);
    await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"delete-confirm-modal\"]'))"));
    await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"delete-confirm-modal\"] footer .secondary-button')?.click()", true);
    await waitFor(() => window.webContents.executeJavaScript("!document.querySelector('[data-ui=\"delete-confirm-modal\"]')"));

    await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"use-for-chat\"]')?.click()", true);
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
    await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"back-to-chat\"]').click()", true);
    await waitFor(() => window.webContents.executeJavaScript("Boolean(document.querySelector('[data-ui=\"chat-panel\"]'))"));
  }
  window.showInactive();
  await window.webContents.executeJavaScript("(() => { const trigger = document.querySelector('[data-ui=\"open-settings\"]'); trigger.focus(); trigger.click(); })()", true);
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"settings-drawer\"]')?.getAttribute('aria-hidden') === 'false'"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"settings-drawer\"]')?.getBoundingClientRect().right <= window.innerWidth"));
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"settings-drawer\"]')?.contains(document.activeElement)"));
  const settingsBackgroundIsolated = await window.webContents.executeJavaScript(`(() => {
    const workspace = document.querySelector('.workspace');
    const drawer = document.querySelector('[data-ui="settings-drawer"]');
    document.querySelector('[data-ui="new-chat"]')?.focus();
    return workspace?.getAttribute('aria-hidden') === 'true'
      && workspace?.hasAttribute('inert')
      && drawer?.contains(document.activeElement);
  })()`, true);
  if (!settingsBackgroundIsolated) throw new Error("Drawer background isolation validation failed");
  await new Promise((resolve) => setTimeout(resolve, 350));
  await saveCapture(window, "settings-1440x920.png");
  await window.webContents.executeJavaScript("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))", true);
  await waitFor(() => window.webContents.executeJavaScript("document.querySelector('[data-ui=\"settings-drawer\"]')?.getAttribute('aria-hidden') === 'true'"));
  await waitFor(() => window.webContents.executeJavaScript("document.activeElement?.matches('[data-ui=\"open-settings\"]')"));
  const settingsBackgroundRestored = await window.webContents.executeJavaScript("!document.querySelector('.workspace')?.hasAttribute('inert') && document.querySelector('.workspace')?.getAttribute('aria-hidden') !== 'true'", true);
  if (!settingsBackgroundRestored) throw new Error("Drawer background restoration validation failed");
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"sidebar\"] [aria-label=\"收起侧栏\"]').click()", true);
  await waitFor(() => window.webContents.executeJavaScript(
    "document.querySelector('[data-ui=\"sidebar\"]')?.getAttribute('aria-hidden') === 'true' && document.querySelector('[data-ui=\"sidebar\"]')?.hasAttribute('inert') && document.activeElement?.matches('[data-ui=\"expand-sidebar\"]')",
  ));
  await window.webContents.executeJavaScript("document.querySelector('[data-ui=\"expand-sidebar\"]').click()", true);
  await waitFor(() => window.webContents.executeJavaScript(
    "document.querySelector('[data-ui=\"sidebar\"]')?.getAttribute('aria-hidden') === 'false' && !document.querySelector('[data-ui=\"sidebar\"]')?.hasAttribute('inert') && document.activeElement?.matches('[data-ui=\"sidebar\"] [aria-label=\"收起侧栏\"]')",
  ));
  await cleanupCaptureArtifacts();
  window.hide();
  window.destroy();
  app.quit();
}).catch(async (error) => {
  console.error(error.message);
  try { await cleanupCaptureArtifacts(); }
  catch (cleanupError) { console.error(cleanupError.message); }
  app.exit(1);
});
