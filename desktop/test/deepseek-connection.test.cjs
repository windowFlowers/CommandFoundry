const assert = require("node:assert/strict");
const test = require("node:test");

const { testDeepSeekConnection } = require("../electron/deepseek-connection.cjs");
const { testOpenAICompatibleConnection } = require("../electron/openai-compatible-connection.cjs");


test("connection test sends a real minimal chat completion and returns evidence", async () => {
  const result = await testDeepSeekConnection("test-key", {
    fetchImpl: async (url, options) => {
      assert.equal(url, "https://api.deepseek.com/v1/chat/completions");
      assert.equal(options.method, "POST");
      const body = JSON.parse(options.body);
      assert.equal(body.model, "deepseek-chat");
      assert.equal(body.messages.length, 2);
      return {
        ok: true,
        json: async () => ({
          id: "chatcmpl-test",
          model: "deepseek-chat",
          usage: { total_tokens: 19 },
          choices: [{ message: { content: '{"status":"ok"}' } }],
        }),
      };
    },
  });
  assert.equal(result.requestId, "chatcmpl-test");
  assert.equal(result.totalTokens, 19);
  assert.ok(result.latencyMs > 0);
});

test("connection test reports authentication and malformed responses", async () => {
  await assert.rejects(
    () => testDeepSeekConnection("bad-key", { fetchImpl: async () => ({ ok: false, status: 401 }) }),
    /认证失败/,
  );
  await assert.rejects(
    () => testDeepSeekConnection("test-key", {
      fetchImpl: async () => ({ ok: true, json: async () => ({ choices: [{ message: { content: "not-json" } }] }) }),
    }),
    /有效 JSON/,
  );
});

test("generic connection test supports a keyless local Ollama endpoint", async () => {
  const result = await testOpenAICompatibleConnection({
    provider: "ollama",
    baseUrl: "http://127.0.0.1:11434/v1/",
    model: "llama3.2",
    fetchImpl: async (url, options) => {
      assert.equal(url, "http://127.0.0.1:11434/v1/chat/completions");
      const body = JSON.parse(options.body);
      assert.equal(body.model, "llama3.2");
      assert.equal(options.headers.Authorization, undefined);
      return {
        ok: true,
        json: async () => ({ choices: [{ message: { content: '{"status":"ok"}' } }] }),
      };
    },
  });
  assert.equal(result.provider, "ollama");
  assert.equal(result.providerLabel, "Ollama（本机）");
});

test("generic connection test retries a portable request for older compatible endpoints", async () => {
  let calls = 0;
  const result = await testOpenAICompatibleConnection({
    provider: "openai",
    apiKey: "test-key",
    baseUrl: "https://api.openai.example/v1",
    model: "gpt-test",
    fetchImpl: async (url, options) => {
      calls += 1;
      const body = JSON.parse(options.body);
      if (calls === 1) {
        assert.equal(body.response_format.type, "json_object");
        return { ok: false, status: 400, body: { error: { message: "response_format is not supported" } } };
      }
      assert.equal(body.response_format, undefined);
      assert.equal(body.temperature, undefined);
      assert.equal(body.max_tokens, undefined);
      return { ok: true, json: async () => ({ choices: [{ message: { content: '{"status":"ok"}' } }] }) };
    },
  });
  assert.equal(calls, 2);
  assert.equal(result.provider, "openai");
});
