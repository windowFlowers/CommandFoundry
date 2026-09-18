const fs = require("node:fs");
const path = require("node:path");

const {
  normalizeBaseUrl,
  normalizeModel,
  normalizeProvider,
  providerPreset,
  providerRequiresApiKey,
} = require("./model-providers.cjs");

const MODEL_CONFIG_VERSION = 2;


function modelConfigPath(userDataPath) {
  return path.join(userDataPath, "model-config.json");
}

function defaultModelConfig() {
  const preset = providerPreset("deepseek");
  return {
    provider: preset.id,
    model: preset.model,
    baseUrl: preset.baseUrl,
    apiKey: "",
  };
}

function normalizeModelConfig(input = {}, existing = defaultModelConfig()) {
  const provider = normalizeProvider(input.provider || existing.provider);
  const preset = providerPreset(provider);
  const model = normalizeModel(input.model || existing.model || preset.model);
  const baseUrl = normalizeBaseUrl(input.baseUrl || existing.baseUrl || preset.baseUrl, { allowEmpty: provider === "custom" });
  const apiKey = String(input.apiKey ?? existing.apiKey ?? "").trim();
  if (providerRequiresApiKey(provider) && !apiKey) throw new Error("当前模型提供商需要 API Key");
  return { provider, model, baseUrl, apiKey };
}

function readModelConfig({ userDataPath, safeStorage, fsImpl = fs }) {
  const filePath = modelConfigPath(userDataPath);
  if (!fsImpl.existsSync(filePath)) return defaultModelConfig();
  try {
    const data = JSON.parse(fsImpl.readFileSync(filePath, "utf8"));
    if (!safeStorage.isEncryptionAvailable() || typeof data.ciphertext !== "string") {
      const provider = normalizeProvider(data.provider);
      const preset = providerPreset(provider);
      return {
        provider,
        model: normalizeModel(data.model || preset.model),
        baseUrl: normalizeBaseUrl(data.baseUrl || preset.baseUrl, { allowEmpty: provider === "custom" }),
        apiKey: "",
      };
    }
    const apiKey = safeStorage.decryptString(Buffer.from(data.ciphertext, "base64"));
    const existing = defaultModelConfig();
    const provider = normalizeProvider(data.provider || existing.provider);
    const preset = providerPreset(provider);
    return {
      provider,
      model: String(data.model || preset.model),
      baseUrl: String(data.baseUrl || preset.baseUrl),
      apiKey: String(apiKey || ""),
    };
  } catch {
    return defaultModelConfig();
  }
}

function saveModelConfig({ userDataPath, apiKey, provider, model, baseUrl, safeStorage, fsImpl = fs }) {
  const existing = readModelConfig({ userDataPath, safeStorage, fsImpl });
  const requestedProvider = normalizeProvider(provider || existing.provider);
  const fallback = requestedProvider === existing.provider ? existing : { ...existing, apiKey: "" };
  const normalized = normalizeModelConfig({ apiKey, provider: requestedProvider, model, baseUrl }, fallback);
  if (!safeStorage.isEncryptionAvailable()) throw new Error("当前系统无法使用安全密钥存储");
  fsImpl.mkdirSync(userDataPath, { recursive: true });
  const filePath = modelConfigPath(userDataPath);
  const temporaryPath = `${filePath}.tmp`;
  const payload = {
    version: MODEL_CONFIG_VERSION,
    provider: normalized.provider,
    model: normalized.model,
    baseUrl: normalized.baseUrl,
    ciphertext: safeStorage.encryptString(normalized.apiKey).toString("base64"),
  };
  fsImpl.writeFileSync(temporaryPath, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 });
  fsImpl.renameSync(temporaryPath, filePath);
}

function clearApiKey({ userDataPath, fsImpl = fs }) {
  const filePath = modelConfigPath(userDataPath);
  if (fsImpl.existsSync(filePath)) fsImpl.rmSync(filePath);
}

function readApiKey(options) {
  return readModelConfig(options).apiKey;
}

function saveApiKey({ userDataPath, apiKey, safeStorage, fsImpl = fs }) {
  const existing = readModelConfig({ userDataPath, safeStorage, fsImpl });
  saveModelConfig({
    userDataPath,
    apiKey,
    provider: existing.provider,
    model: existing.model,
    baseUrl: existing.baseUrl,
    safeStorage,
    fsImpl,
  });
}

function resolveConnectionTestApiKey({ input = {}, current = {} } = {}) {
  const explicit = String(input.apiKey ?? "").trim();
  if (explicit) return explicit;
  const requestedProvider = normalizeProvider(input.provider || current.provider);
  return requestedProvider === normalizeProvider(current.provider)
    ? String(current.apiKey || "").trim()
    : "";
}

module.exports = {
  MODEL_CONFIG_VERSION,
  clearApiKey,
  defaultModelConfig,
  modelConfigPath,
  normalizeModelConfig,
  readApiKey,
  readModelConfig,
  resolveConnectionTestApiKey,
  saveApiKey,
  saveModelConfig,
};
