const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { clearApiKey, modelConfigPath, readApiKey, saveApiKey } = require("../electron/model-config.cjs");


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

test("preload exposes only scoped model config methods", () => {
  const preload = fs.readFileSync(path.resolve(__dirname, "../electron/preload.cjs"), "utf8");
  assert.match(preload, /modelConfig/);
  assert.match(preload, /model-config:status/);
  assert.equal(/decryptString|readApiKey/.test(preload), false);
});
