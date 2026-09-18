const PROVIDER_PRESETS = Object.freeze({
  deepseek: Object.freeze({
    id: "deepseek",
    label: "DeepSeek",
    baseUrl: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
    requiresApiKey: true,
  }),
  openai: Object.freeze({
    id: "openai",
    label: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
    requiresApiKey: true,
  }),
  qwen: Object.freeze({
    id: "qwen",
    label: "通义千问 / DashScope",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen-plus",
    requiresApiKey: true,
  }),
  moonshot: Object.freeze({
    id: "moonshot",
    label: "Moonshot / Kimi",
    baseUrl: "https://api.moonshot.cn/v1",
    model: "moonshot-v1-8k",
    requiresApiKey: true,
  }),
  siliconflow: Object.freeze({
    id: "siliconflow",
    label: "SiliconFlow",
    baseUrl: "https://api.siliconflow.cn/v1",
    model: "deepseek-ai/DeepSeek-V3",
    requiresApiKey: true,
  }),
  ollama: Object.freeze({
    id: "ollama",
    label: "Ollama（本机）",
    baseUrl: "http://127.0.0.1:11434/v1",
    model: "llama3.2",
    requiresApiKey: false,
  }),
  custom: Object.freeze({
    id: "custom",
    label: "自定义 OpenAI 兼容 API",
    baseUrl: "",
    model: "",
    requiresApiKey: true,
  }),
});

const PROVIDER_OPTIONS = Object.freeze(
  Object.values(PROVIDER_PRESETS).map(({ id, label }) => ({ value: id, label })),
);

function providerPreset(provider) {
  return PROVIDER_PRESETS[String(provider || "").trim().toLowerCase()] || PROVIDER_PRESETS.custom;
}

function providerLabel(provider) {
  return providerPreset(provider).label;
}

function providerRequiresApiKey(provider) {
  return providerPreset(provider).requiresApiKey;
}

function normalizeBaseUrl(value, { allowEmpty = false } = {}) {
  const raw = String(value || "").trim().replace(/\/+$/, "");
  if (!raw && allowEmpty) return "";
  if (!raw) throw new Error("API Base URL 不能为空");
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("API Base URL 必须是完整的 http(s) 地址");
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error("API Base URL 只支持 http 或 https");
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("API Base URL 不能包含凭证、查询参数或片段");
  }
  return raw;
}

function normalizeModel(value) {
  const model = String(value || "").trim();
  if (!model || model.length > 160) throw new Error("模型名称不能为空且不能超过 160 个字符");
  return model;
}

function normalizeProvider(value) {
  const provider = String(value || "deepseek").trim().toLowerCase();
  if (!Object.prototype.hasOwnProperty.call(PROVIDER_PRESETS, provider)) return "custom";
  return provider;
}

module.exports = {
  PROVIDER_PRESETS,
  PROVIDER_OPTIONS,
  normalizeBaseUrl,
  normalizeModel,
  normalizeProvider,
  providerLabel,
  providerPreset,
  providerRequiresApiKey,
};
