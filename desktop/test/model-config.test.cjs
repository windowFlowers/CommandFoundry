const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  clearApiKey,
  modelConfigPath,
  readApiKey,
  readModelConfig,
  resolveConnectionTestApiKey,
  saveApiKey,
  saveModelConfig,
} = require("../electron/model-config.cjs");


function fakeSafeStorage() {
  return {
    isEncryptionAvailable: () => true,
    encryptString: (value) => Buffer.from(`encrypted:${value}`, "utf8"),
    decryptString: (buffer) => buffer.toString("utf8").replace(/^encrypted:/, ""),
  };
}

test("model API key is encrypted at rest and can be cleared", () => {
  const userDataPath = fs.mkdtempSync(path.join(os.tmpdir(), "aegis-config-"));
  const safeStorage = fakeSafeStorage();
  saveApiKey({ userDataPath, apiKey: "test-secret", safeStorage });
  const raw = fs.readFileSync(modelConfigPath(userDataPath), "utf8");
  assert.equal(raw.includes("test-secret"), false);
  assert.equal(readApiKey({ userDataPath, safeStorage }), "test-secret");
  clearApiKey({ userDataPath });
  assert.equal(readApiKey({ userDataPath, safeStorage }), "");
});

test("model config refuses plaintext fallback when encryption is unavailable", () => {
  const userDataPath = fs.mkdtempSync(path.join(os.tmpdir(), "aegis-config-"));
  const safeStorage = { isEncryptionAvailable: () => false };
  assert.throws(
    () => saveApiKey({ userDataPath, apiKey: "test-secret", safeStorage }),
    /安全密钥存储/,
  );
  assert.equal(fs.existsSync(modelConfigPath(userDataPath)), false);
});

test("model config stores an OpenAI-compatible provider without exposing the key", () => {
  const userDataPath = fs.mkdtempSync(path.join(os.tmpdir(), "aegis-config-provider-"));
  const safeStorage = fakeSafeStorage();
  saveModelConfig({
    userDataPath,
    safeStorage,
    apiKey: "provider-secret",
    provider: "openai",
    model: "gpt-test",
    baseUrl: "https://api.example.test/v1",
  });
  const raw = fs.readFileSync(modelConfigPath(userDataPath), "utf8");
  assert.equal(raw.includes("provider-secret"), false);
  assert.deepEqual(readModelConfig({ userDataPath, safeStorage }), {
    provider: "openai",
    model: "gpt-test",
    baseUrl: "https://api.example.test/v1",
    apiKey: "provider-secret",
  });
  assert.equal(readApiKey({ userDataPath, safeStorage }), "provider-secret");
});

test("connection tests do not reuse a saved key after switching provider", () => {
  const current = { provider: "openai", apiKey: "openai-secret" };
  assert.equal(
    resolveConnectionTestApiKey({ input: { provider: "openai" }, current }),
    "openai-secret",
  );
  assert.equal(
    resolveConnectionTestApiKey({ input: { provider: "deepseek" }, current }),
    "",
  );
  assert.equal(
    resolveConnectionTestApiKey({ input: { provider: "deepseek", apiKey: "new-secret" }, current }),
    "new-secret",
  );
});

test("unencrypted legacy provider config keeps its own preset defaults", () => {
  const userDataPath = fs.mkdtempSync(path.join(os.tmpdir(), "aegis-config-legacy-"));
  const filePath = modelConfigPath(userDataPath);
  fs.writeFileSync(filePath, JSON.stringify({ version: 1, provider: "openai", ciphertext: "ignored" }));
  const config = readModelConfig({
    userDataPath,
    safeStorage: { isEncryptionAvailable: () => false },
  });
  assert.equal(config.provider, "openai");
  assert.equal(config.model, "gpt-4o-mini");
  assert.equal(config.baseUrl, "https://api.openai.com/v1");
  assert.equal(config.apiKey, "");
});

test("preload exposes only scoped model config methods", () => {
  const preload = fs.readFileSync(path.resolve(__dirname, "../electron/preload.cjs"), "utf8");
  assert.match(preload, /modelConfig/);
  assert.match(preload, /provider: input\.provider/);
  assert.match(preload, /baseUrl: input\.baseUrl/);
  assert.match(preload, /model-config:status/);
  assert.match(preload, /terminal:\s*\{/);
  assert.match(preload, /terminal:create/);
  assert.match(preload, /terminal:write/);
  assert.match(preload, /terminal:resize/);
  assert.match(preload, /terminal:close/);
  assert.match(preload, /terminal:list/);
  assert.match(preload, /terminal:data/);
  assert.match(preload, /terminal:exit/);
  assert.match(preload, /terminal:error/);
  assert.equal(/child_process|execFile|spawn\(/.test(preload), false);
  assert.equal(/decryptString|readApiKey/.test(preload), false);
});
