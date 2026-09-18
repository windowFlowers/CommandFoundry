const OpenAI = require("openai");

const {
  normalizeBaseUrl,
  normalizeModel,
  normalizeProvider,
  providerLabel,
  providerRequiresApiKey,
} = require("./model-providers.cjs");

async function normalizeFetchResponse(result) {
  if (result instanceof Response) return result;
  let body = "{}";
  if (result && typeof result.text === "function") {
    body = await result.text();
  } else if (result && typeof result.json === "function") {
    body = JSON.stringify(await result.json());
  } else if (result && result.body !== undefined) {
    body = typeof result.body === "string" ? result.body : JSON.stringify(result.body);
  }
  return new Response(body, {
    status: Number(result?.status || (result?.ok === false ? 500 : 200)),
    headers: { "content-type": "application/json" },
  });
}

function adaptFetch(fetchImpl) {
  if (typeof fetchImpl !== "function" || fetchImpl === globalThis.fetch) return undefined;
  return async (input, init = {}) => {
    const sourceHeaders = new Headers(init.headers || {});
    const headers = Object.fromEntries(sourceHeaders.entries());
    const result = await fetchImpl(typeof input === "string" ? input : input.url, {
      ...init,
      headers,
    });
    return normalizeFetchResponse(result);
  };
}

function errorForProvider(label, error) {
  const status = Number(error?.status || error?.statusCode || 0);
  if ([401, 403].includes(status)) return new Error(`${label} API Key 认证失败`);
  if (status === 429) return new Error(`${label} 请求限流`);
  if (error?.name === "APIConnectionTimeoutError") return new Error("连接超时，请检查网络后重试");
  if (error?.name === "APIConnectionError" && /timed out|timeout/i.test(String(error?.message || ""))) {
    return new Error("连接超时，请检查网络后重试");
  }
  if (status) return new Error(`${label} 返回 HTTP ${status}`);
  return error;
}

function shouldRetryPortableRequest(error) {
  const status = Number(error?.status || error?.statusCode || 0);
  if (status !== 400) return false;
  const message = String(error?.message || error?.body || error || "").toLowerCase();
  return [
    "unsupported",
    "not support",
    "unknown parameter",
    "unrecognized request argument",
    "temperature",
    "max_tokens",
    "response_format",
  ].some((marker) => message.includes(marker));
}

async function testOpenAICompatibleConnection({
  apiKey = "",
  baseUrl,
  model,
  provider = "custom",
  fetchImpl = globalThis.fetch,
  timeoutMs = 15_000,
  clientFactory = (options) => new OpenAI(options),
} = {}) {
  const normalizedProvider = normalizeProvider(provider);
  const label = providerLabel(normalizedProvider);
  const key = String(apiKey || "").trim();
  if (providerRequiresApiKey(normalizedProvider) && !key) throw new Error("请先输入或保存 API Key");
  const normalizedBaseUrl = normalizeBaseUrl(baseUrl);
  const normalizedModel = normalizeModel(model);
  const client = clientFactory({
    apiKey: key || "ollama",
    baseURL: normalizedBaseUrl,
    maxRetries: 0,
    timeout: timeoutMs,
    fetch: adaptFetch(fetchImpl),
  });
  const started = Date.now();
  try {
    const request = {
      model: normalizedModel,
      temperature: 0,
      max_tokens: 32,
      response_format: { type: "json_object" },
      messages: [
        { role: "system", content: "Return strict JSON only." },
        { role: "user", content: 'Return exactly {"status":"ok"}.' },
      ],
    };
    let response;
    try {
      response = await client.chat.completions.create(request);
    } catch (error) {
      if (!shouldRetryPortableRequest(error)) throw error;
      const portable = { ...request };
      delete portable.temperature;
      delete portable.max_tokens;
      delete portable.response_format;
      response = await client.chat.completions.create(portable);
    }
    const content = response?.choices?.[0]?.message?.content;
    let structured;
    try {
      structured = JSON.parse(content);
    } catch {
      throw new Error(`${label} 未返回有效 JSON`);
    }
    if (structured?.status !== "ok") throw new Error(`${label} 结构化响应校验失败`);
    const tokens = Number(response?.usage?.total_tokens || 0);
    const latency = Math.max(1, Date.now() - started);
    return {
      ok: true,
      provider: normalizedProvider,
      providerLabel: label,
      requestId: response?.id || null,
      model: response?.model || normalizedModel,
      totalTokens: tokens || null,
      latencyMs: latency,
      message: `${label} 连接成功 · ${tokens || "无 usage"} token · ${latency} ms`,
    };
  } catch (error) {
    throw errorForProvider(label, error);
  } finally {
    if (client && typeof client.close === "function") client.close();
  }
}

module.exports = { testOpenAICompatibleConnection };
