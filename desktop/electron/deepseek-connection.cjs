const { testOpenAICompatibleConnection } = require("./openai-compatible-connection.cjs");

// Backward-compatible export for existing integrations and tests. DeepSeek is
// now one preset of the generic OpenAI-compatible connection tester.
function testDeepSeekConnection(apiKey, options = {}) {
  return testOpenAICompatibleConnection({
    apiKey,
    provider: "deepseek",
    baseUrl: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
    ...options,
  });
}

module.exports = { testDeepSeekConnection };
