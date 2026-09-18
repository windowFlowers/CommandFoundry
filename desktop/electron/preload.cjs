const { contextBridge, ipcRenderer } = require("electron");

const apiBaseArgument = process.argv.find((argument) => argument.startsWith("--aegis-api-base="));
const versionArgument = process.argv.find((argument) => argument.startsWith("--aegis-version="));

contextBridge.exposeInMainWorld("aegisDesktop", {
  apiBaseUrl: apiBaseArgument ? apiBaseArgument.slice("--aegis-api-base=".length) : "",
  isPackaged: process.argv.includes("--aegis-packaged=true"),
  version: versionArgument ? versionArgument.slice("--aegis-version=".length) : "2.10.0",
  modelConfig: {
    status: () => ipcRenderer.invoke("model-config:status"),
    test: (input = {}) => ipcRenderer.invoke("model-config:test", {
      apiKey: input.apiKey,
      provider: input.provider,
      model: input.model,
      baseUrl: input.baseUrl,
    }),
    save: (input = {}) => ipcRenderer.invoke("model-config:save", {
      apiKey: input.apiKey,
      provider: input.provider,
      model: input.model,
      baseUrl: input.baseUrl,
    }),
    clear: () => ipcRenderer.invoke("model-config:clear"),
  },
  terminal: {
    create: (input = {}) => ipcRenderer.invoke("terminal:create", {
      shell: input.shell,
      cwd: input.cwd,
    }),
    write: (input = {}) => ipcRenderer.invoke("terminal:write", {
      terminalId: input.terminalId,
      data: input.data,
    }),
    resize: (input = {}) => ipcRenderer.invoke("terminal:resize", {
      terminalId: input.terminalId,
      cols: input.cols,
      rows: input.rows,
    }),
    close: (input = {}) => ipcRenderer.invoke("terminal:close", {
      terminalId: input.terminalId,
    }),
    list: () => ipcRenderer.invoke("terminal:list"),
    on: (eventName, listener) => {
      const allowed = new Set(["terminal:data", "terminal:exit", "terminal:error"]);
      if (!allowed.has(eventName) || typeof listener !== "function") return () => {};
      const handler = (_, payload) => listener(payload);
      ipcRenderer.on(eventName, handler);
      return () => ipcRenderer.removeListener(eventName, handler);
    },
  },
});
