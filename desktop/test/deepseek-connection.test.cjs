const assert = require("node:assert/strict");
const test = require("node:test");

const { testDeepSeekConnection } = require("../electron/deepseek-connection.cjs");


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
