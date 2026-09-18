const path = require("node:path");
const { app, safeStorage } = require("electron");

const { testOpenAICompatibleConnection } = require("../electron/openai-compatible-connection.cjs");
const { readModelConfig } = require("../electron/model-config.cjs");

app.setPath("userData", path.join(app.getPath("appData"), "AegisCopilot"));

app.whenReady().then(async () => {
  const config = readModelConfig({ userDataPath: app.getPath("userData"), safeStorage });
  if (!config.apiKey && config.provider !== "ollama") {
    process.stdout.write(JSON.stringify({ ok: false, configured: false, provider: config.provider, reason: "no_api_key" }));
    app.exit(2);
    return;
  }
  try {
    const result = await testOpenAICompatibleConnection(config);
    process.stdout.write(JSON.stringify({ configured: true, ...result }));
    app.exit(0);
  } catch (error) {
    process.stdout.write(JSON.stringify({ ok: false, configured: true, provider: config.provider, error: error.message }));
    app.exit(1);
  }
});
