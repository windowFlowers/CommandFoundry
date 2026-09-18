const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  protocol,
  safeStorage,
  shell,
} = require("electron");

const {
  clearApiKey,
  readModelConfig,
  resolveConnectionTestApiKey,
  saveModelConfig,
} = require("./model-config.cjs");
const { testOpenAICompatibleConnection } = require("./openai-compatible-connection.cjs");
const { normalizeProvider, providerLabel } = require("./model-providers.cjs");
const {
  buildBackendLaunchSpec,
  buildRuntimeEnvironment,
  buildViteLaunchSpec,
  extractLocalPort,
  findFreePort,
  stopProcessTree,
  waitForHealth,
} = require("./process-manager.cjs");
const { registerAppProtocol } = require("./protocol.cjs");
const { TerminalManager } = require("./terminal-manager.cjs");

app.setAppUserModelId("com.aegiscopilot.desktop");
app.setPath("userData", path.join(app.getPath("appData"), "AegisCopilot"));

protocol.registerSchemesAsPrivileged([
  {
    scheme: "aegis",
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      stream: true,
      corsEnabled: true,
    },
  },
]);

const PROJECT_ROOT = path.resolve(__dirname, "..", "..");
const LOOPBACK_HOST = "127.0.0.1";
let mainWindow = null;
let backendProcess = null;
let frontendProcess = null;
let runtime = null;
let shuttingDown = false;
let servicesReady = false;
let terminalManager = null;

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) app.quit();

function frontendDistRoot() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "frontend")
    : path.join(PROJECT_ROOT, "frontend", "dist");
}

function logDirectory() {
  const directory = path.join(app.getPath("userData"), "logs");
  fs.mkdirSync(directory, { recursive: true });
  return directory;
}

function attachProcessLog(child, name) {
  const stream = fs.createWriteStream(path.join(logDirectory(), `${name}.log`), { flags: "a" });
  child.stdout?.pipe(stream);
  child.stderr?.pipe(stream);
  child.once("close", () => stream.end());
}

function waitForSpawn(child, label) {
  return new Promise((resolve, reject) => {
    child.once("spawn", resolve);
    child.once("error", (error) => reject(new Error(`${label}启动失败：${error.message}`)));
  });
}

function secureModelConfig() {
  return readModelConfig({ userDataPath: app.getPath("userData"), safeStorage });
}

async function startBackend() {
  const port = runtime?.backendPort || (await findFreePort());
  const spec = buildBackendLaunchSpec({
    projectRoot: PROJECT_ROOT,
    packaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    port,
  });
  const modelConfig = secureModelConfig();
  const useEnvironmentModel = !modelConfig.apiKey
    && modelConfig.provider !== "ollama"
    && Boolean(process.env.AEGIS_LLM_API_KEY);
  const baseEnv = {
    ...process.env,
    AEGIS_LLM_API_KEY: useEnvironmentModel ? process.env.AEGIS_LLM_API_KEY : modelConfig.apiKey || "",
    AEGIS_LLM_PROVIDER: useEnvironmentModel ? process.env.AEGIS_LLM_PROVIDER || "deepseek" : modelConfig.provider,
    AEGIS_LLM_MODEL: useEnvironmentModel ? process.env.AEGIS_LLM_MODEL || "deepseek-chat" : modelConfig.model,
    AEGIS_LLM_BASE_URL: useEnvironmentModel ? process.env.AEGIS_LLM_BASE_URL || "https://api.deepseek.com/v1" : modelConfig.baseUrl,
  };
  const environment = app.isPackaged
    ? buildRuntimeEnvironment({
        userDataPath: app.getPath("userData"),
        resourcesPath: process.resourcesPath,
        baseEnv,
      })
    : {
        ...baseEnv,
        AEGIS_STORAGE_DIR: path.join(app.getPath("userData"), "storage"),
        AEGIS_KNOWLEDGE_DIR: path.join(PROJECT_ROOT, "knowledge"),
        AEGIS_MODEL_DIR: path.join(PROJECT_ROOT, "models", "cache"),
      };

  backendProcess = spawn(spec.command, spec.args, {
    cwd: spec.cwd,
    env: environment,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  attachProcessLog(backendProcess, "backend");
  backendProcess.once("exit", () => handleUnexpectedServiceExit("后端"));
  await waitForSpawn(backendProcess, "后端");
  await waitForHealth(`http://${LOOPBACK_HOST}:${port}/health`, { timeoutMs: 90_000 });
  runtime = { ...(runtime || {}), backendPort: port, apiBaseUrl: `http://${LOOPBACK_HOST}:${port}` };
}

function startVite() {
  const spec = buildViteLaunchSpec({ projectRoot: PROJECT_ROOT });
  let output = "";
  const portPromise = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Vite 在 60 秒内没有报告可访问地址")), 60_000);
    const handleOutput = (chunk) => {
      output += chunk.toString();
      const port = extractLocalPort(output);
      if (port) {
        clearTimeout(timeout);
        resolve(port);
      }
    };
    frontendProcess = spawn(spec.command, spec.args, {
      cwd: spec.cwd,
      env: { ...process.env, VITE_API_PROXY_TARGET: runtime.apiBaseUrl },
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    frontendProcess.stdout?.on("data", handleOutput);
    frontendProcess.stderr?.on("data", handleOutput);
    frontendProcess.once("error", (error) => {
      clearTimeout(timeout);
      reject(new Error(`Vite 启动失败：${error.message}`));
    });
    frontendProcess.once("exit", (code) => {
      if (code && !shuttingDown) reject(new Error(`Vite 退出，code=${code}`));
    });
    attachProcessLog(frontendProcess, "frontend");
    frontendProcess.once("exit", () => handleUnexpectedServiceExit("前端"));
  });
  return portPromise;
}

async function startServices() {
  runtime = null;
  await startBackend();
  runtime.frontendPort = app.isPackaged ? null : await startVite();
  servicesReady = true;
}

async function stopServices() {
  servicesReady = false;
  const processes = [frontendProcess, backendProcess].filter(Boolean);
  frontendProcess = null;
  backendProcess = null;
  await Promise.all(processes.map((child) => stopProcessTree(child)));
}

async function restartBackend() {
  servicesReady = false;
  const processToStop = backendProcess;
  backendProcess = null;
  if (processToStop) await stopProcessTree(processToStop);
  await startBackend();
  servicesReady = true;
}

async function handleUnexpectedServiceExit(label) {
  if (shuttingDown || !servicesReady) return;
  servicesReady = false;
  const result = await dialog.showMessageBox(mainWindow, {
    type: "error",
    title: "AegisCopilot 服务已停止",
    message: `${label}进程意外退出。`,
    detail: `可以重启应用恢复。日志目录：${logDirectory()}`,
    buttons: ["重启应用", "退出"],
    defaultId: 0,
    cancelId: 1,
  });
  if (result.response === 0) app.relaunch();
  app.quit();
}

function isAllowedNavigation(url) {
  if (app.isPackaged) return url.startsWith("aegis://app/");
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" && ["127.0.0.1", "localhost"].includes(parsed.hostname) && parsed.port === String(runtime.frontendPort);
  } catch {
    return false;
  }
}

function createMainWindow() {
  Menu.setApplicationMenu(null);
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1080,
    minHeight: 720,
    show: false,
    backgroundColor: "#efeee9",
    icon: path.join(__dirname, "..", "assets", "icon.ico"),
    titleBarStyle: "hidden",
    titleBarOverlay: { color: "#e5e2da", symbolColor: "#232626", height: 38 },
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.cjs"),
      additionalArguments: [
        `--aegis-api-base=${runtime.apiBaseUrl}`,
        `--aegis-packaged=${app.isPackaged}`,
        `--aegis-version=${app.getVersion()}`,
      ],
    },
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https:\/\//i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!isAllowedNavigation(url)) {
      event.preventDefault();
      if (/^https:\/\//i.test(url)) shell.openExternal(url);
    }
  });
  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.on("closed", () => { mainWindow = null; });
  return mainWindow;
}

async function loadMainWindow() {
  const window = createMainWindow();
  if (app.isPackaged) {
    registerAppProtocol(protocol, frontendDistRoot());
    await window.loadURL("aegis://app/");
  } else {
    await window.loadURL(`http://${LOOPBACK_HOST}:${runtime.frontendPort}/`);
  }
}

function registerModelConfigIpc() {
  ipcMain.handle("model-config:status", () => {
    const config = secureModelConfig();
    return {
      configured: Boolean(config.apiKey) || config.provider === "ollama",
      encryptionAvailable: safeStorage.isEncryptionAvailable(),
      provider: config.provider,
      providerLabel: providerLabel(config.provider),
      model: config.model,
      baseUrl: config.baseUrl,
    };
  });
  ipcMain.handle("model-config:test", (_, input = {}) => {
    const current = secureModelConfig();
    const requestedProvider = normalizeProvider(input.provider || current.provider);
    return testOpenAICompatibleConnection({
      // A saved key is scoped to its provider.  Never send an OpenAI key to a
      // newly selected DeepSeek/Ollama/custom endpoint during a connection test.
      apiKey: resolveConnectionTestApiKey({ input, current }),
      provider: requestedProvider,
      model: input.model || current.model,
      baseUrl: input.baseUrl || current.baseUrl,
    });
  });
  ipcMain.handle("model-config:save", async (_, input = {}) => {
    saveModelConfig({
      userDataPath: app.getPath("userData"),
      apiKey: input.apiKey,
      provider: input.provider,
      model: input.model,
      baseUrl: input.baseUrl,
      safeStorage,
    });
    await restartBackend();
    return { ok: true };
  });
  ipcMain.handle("model-config:clear", async () => {
    clearApiKey({ userDataPath: app.getPath("userData") });
    await restartBackend();
    return { ok: true };
  });
}

function registerTerminalIpc() {
  terminalManager = new TerminalManager();
  terminalManager.on("data", (payload) => mainWindow?.webContents.send("terminal:data", payload));
  terminalManager.on("exit", (payload) => mainWindow?.webContents.send("terminal:exit", payload));
  terminalManager.on("error", (payload) => mainWindow?.webContents.send("terminal:error", payload));
  ipcMain.handle("terminal:create", (_, input = {}) => terminalManager.create(input));
  ipcMain.handle("terminal:write", (_, input = {}) => terminalManager.write(input));
  ipcMain.handle("terminal:resize", (_, input = {}) => terminalManager.resize(input));
  ipcMain.handle("terminal:close", (_, input = {}) => terminalManager.close(input));
  ipcMain.handle("terminal:list", () => terminalManager.list());
}

async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  terminalManager?.closeAll();
  await stopServices();
}

async function bootstrap() {
  if (!hasSingleInstanceLock) return;
  try {
    registerModelConfigIpc();
    registerTerminalIpc();
    await startServices();
    await loadMainWindow();
  } catch (error) {
    await dialog.showMessageBox({
      type: "error",
      title: "AegisCopilot 启动失败",
      message: error.message,
      detail: `日志目录：${logDirectory()}`,
      buttons: ["退出"],
    });
    await shutdown();
    app.exit(1);
  }
}

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
});
app.on("before-quit", (event) => {
  if (shuttingDown) return;
  event.preventDefault();
  shutdown().finally(() => app.exit(0));
});
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
app.whenReady().then(bootstrap);
