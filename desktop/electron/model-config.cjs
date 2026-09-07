const fs = require("node:fs");
const path = require("node:path");


function modelConfigPath(userDataPath) {
  return path.join(userDataPath, "model-config.json");
}

function readApiKey({ userDataPath, safeStorage, fsImpl = fs }) {
  const filePath = modelConfigPath(userDataPath);
  if (!fsImpl.existsSync(filePath) || !safeStorage.isEncryptionAvailable()) return "";
  try {
    const data = JSON.parse(fsImpl.readFileSync(filePath, "utf8"));
    if (data.version !== 1 || typeof data.ciphertext !== "string") return "";
    return safeStorage.decryptString(Buffer.from(data.ciphertext, "base64"));
  } catch {
    return "";
  }
}

function saveApiKey({ userDataPath, apiKey, safeStorage, fsImpl = fs }) {
  const normalized = String(apiKey || "").trim();
  if (!normalized) throw new Error("API Key 不能为空");
  if (!safeStorage.isEncryptionAvailable()) throw new Error("当前系统无法使用安全密钥存储");
  fsImpl.mkdirSync(userDataPath, { recursive: true });
  const filePath = modelConfigPath(userDataPath);
  const temporaryPath = `${filePath}.tmp`;
  const payload = {
    version: 1,
    ciphertext: safeStorage.encryptString(normalized).toString("base64"),
  };
  fsImpl.writeFileSync(temporaryPath, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 });
  fsImpl.renameSync(temporaryPath, filePath);
}

function clearApiKey({ userDataPath, fsImpl = fs }) {
  const filePath = modelConfigPath(userDataPath);
  if (fsImpl.existsSync(filePath)) fsImpl.rmSync(filePath);
}

module.exports = { clearApiKey, modelConfigPath, readApiKey, saveApiKey };
