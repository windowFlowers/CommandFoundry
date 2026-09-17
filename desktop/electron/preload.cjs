const { contextBridge, ipcRenderer } = require("electron");

const apiBaseArgument = process.argv.find((argument) => argument.startsWith("--aegis-api-base="));
const versionArgument = process.argv.find((argument) => argument.startsWith("--aegis-version="));

contextBridge.exposeInMainWorld("aegisDesktop", {
  apiBaseUrl: apiBaseArgument ? apiBaseArgument.slice("--aegis-api-base=".length) : "",
  isPackaged: process.argv.includes("--aegis-packaged=true"),
  version: versionArgument ? versionArgument.slice("--aegis-version=".length) : "2.5.0",
  modelConfig: {
    status: () => ipcRenderer.invoke("model-config:status"),
    test: (input = {}) => ipcRenderer.invoke("model-config:test", { apiKey: input.apiKey }),
    save: (input = {}) => ipcRenderer.invoke("model-config:save", { apiKey: input.apiKey }),
    clear: () => ipcRenderer.invoke("model-config:clear"),
  },
});
