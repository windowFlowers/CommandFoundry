async function testDeepSeekConnection(apiKey, { fetchImpl = globalThis.fetch, timeoutMs = 15_000 } = {}) {
  const key = String(apiKey || "").trim();
  if (!key) throw new Error("请先输入或保存 API Key");
  if (typeof fetchImpl !== "function") throw new Error("当前环境不支持网络请求");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const started = Date.now();
  try {
    const response = await fetchImpl("https://api.deepseek.com/v1/chat/completions", {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "deepseek-chat",
        temperature: 0,
        max_tokens: 32,
        response_format: { type: "json_object" },
        messages: [
          { role: "system", content: "Return strict JSON only." },
          { role: "user", content: "Return exactly {\"status\":\"ok\"}." },
        ],
      }),
      signal: controller.signal,
    });
    if (!response.ok) {
      if ([401, 403].includes(response.status)) throw new Error("API Key 认证失败");
      if (response.status === 429) throw new Error("DeepSeek 请求限流");
      throw new Error(`DeepSeek 返回 HTTP ${response.status}`);
    }
    const data = await response.json();
    const content = data?.choices?.[0]?.message?.content;
    let structured;
    try { structured = JSON.parse(content); } catch { throw new Error("DeepSeek 未返回有效 JSON"); }
    if (structured?.status !== "ok") throw new Error("DeepSeek 结构化响应校验失败");
    const tokens = Number(data?.usage?.total_tokens || 0);
    const latency = Math.max(1, Date.now() - started);
    return {
      ok: true,
      requestId: data?.id || null,
      model: data?.model || "deepseek-chat",
      totalTokens: tokens || null,
      latencyMs: latency,
      message: `真实对话成功 · ${tokens || "无 usage"} token · ${latency} ms`,
    };
  } catch (error) {
    if (error.name === "AbortError") throw new Error("连接超时，请检查网络后重试");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

module.exports = { testDeepSeekConnection };
