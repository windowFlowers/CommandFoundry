const path = require("node:path");
const { app, safeStorage } = require("electron");

const { testDeepSeekConnection } = require("../electron/deepseek-connection.cjs");
const { readApiKey } = require("../electron/model-config.cjs");

app.setPath("userData", path.join(app.getPath("appData"), "AegisCopilot"));

app.whenReady().then(async () => {
  const apiKey = readApiKey({ userDataPath: app.getPath("userData"), safeStorage });
  if (!apiKey) {
    process.stdout.write(JSON.stringify({ ok: false, configured: false, reason: "no_api_key" }));
    app.exit(2);
    return;
  }
  try {
    const result = await testDeepSeekConnection(apiKey);
    process.stdout.write(JSON.stringify({ configured: true, ...result }));
    app.exit(0);
  } catch (error) {
    process.stdout.write(JSON.stringify({ ok: false, configured: true, error: error.message }));
    app.exit(1);
  }
});
