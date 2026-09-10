const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { app, safeStorage } = require("electron");

const { readApiKey } = require("../electron/model-config.cjs");
const { findFreePort, stopProcessTree, waitForHealth } = require("../electron/process-manager.cjs");

const projectRoot = path.resolve(__dirname, "..", "..");
app.setPath("userData", path.join(app.getPath("appData"), "AegisCopilot"));

function parseAnswer(stream) {
  for (const frame of stream.split(/\r?\n\r?\n/)) {
    if (!frame.includes("event: answer")) continue;
    const line = frame.split(/\r?\n/).find((value) => value.startsWith("data: "));
    if (line) return JSON.parse(line.slice(6)).answer;
  }
  return null;
}

app.whenReady().then(async () => {
  const apiKey = readApiKey({ userDataPath: app.getPath("userData"), safeStorage });
  if (!apiKey) {
    process.stdout.write(JSON.stringify({ ok: false, configured: false, reason: "no_api_key" }));
    app.exit(2);
    return;
  }
  const storage = fs.mkdtempSync(path.join(os.tmpdir(), "aegis-rag-verify-"));
  const port = await findFreePort();
  const backend = spawn(path.join(projectRoot, "backend", ".venv", "Scripts", "python.exe"), [
    "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port),
  ], {
    cwd: path.join(projectRoot, "backend"),
    windowsHide: true,
    stdio: "ignore",
    env: {
      ...process.env,
      AEGIS_LLM_API_KEY: apiKey,
      AEGIS_EMBEDDING_ENABLED: "0",
      AEGIS_STORAGE_DIR: storage,
      AEGIS_KNOWLEDGE_DIR: path.join(projectRoot, "knowledge"),
    },
  });
  try {
    await waitForHealth(`http://127.0.0.1:${port}/health`, { timeoutMs: 30_000 });
    const response = await fetch(`http://127.0.0.1:${port}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: "Git 怎么安全回滚一次提交？", knowledge_base_id: "developer-it" }),
    });
    const answer = parseAnswer(await response.text());
    if (!answer || answer.mode !== "model") throw new Error(`完整链路未返回模型回答：${answer?.generation?.fallback_reason || "missing_answer"}`);
    if (!answer.generation?.request_id && !answer.generation?.total_tokens) throw new Error("模型回答缺少请求证据");
    if (!answer.commands?.length) throw new Error("模型回答没有通过当前知识证据校验的命令");
    process.stdout.write(JSON.stringify({
      ok: true,
      mode: answer.mode,
      provider: answer.generation.provider,
      model: answer.generation.model,
      requestId: answer.generation.request_id,
      totalTokens: answer.generation.total_tokens,
      latencyMs: answer.generation.latency_ms,
      commandCount: answer.commands.length,
      citationCount: answer.citations.length,
    }));
  } catch (error) {
    process.stdout.write(JSON.stringify({ ok: false, configured: true, error: error.message }));
    process.exitCode = 1;
  } finally {
    await stopProcessTree(backend);
    const resolved = path.resolve(storage);
    if (resolved.startsWith(path.resolve(os.tmpdir()) + path.sep)) fs.rmSync(resolved, { recursive: true, force: true });
    app.exit(process.exitCode || 0);
  }
});
