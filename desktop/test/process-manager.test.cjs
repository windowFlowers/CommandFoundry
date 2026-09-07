const test = require("node:test");
const assert = require("node:assert/strict");

const {
  buildBackendLaunchSpec,
  buildRuntimeEnvironment,
  buildViteLaunchSpec,
  extractLocalPort,
  findFreePort,
} = require("../electron/process-manager.cjs");

test("findFreePort returns a bindable loopback port", async () => {
  const port = await findFreePort();

  assert.equal(Number.isInteger(port), true);
  assert.ok(port > 0);
  assert.ok(port <= 65535);
});

test("development launch spec uses the project virtualenv", () => {
  const projectRoot = "C:\\workspace\\aegiscopilot";
  const spec = buildBackendLaunchSpec({
    projectRoot,
    packaged: false,
    port: 8123,
  });

  assert.match(spec.command, /backend[\\/]\.venv[\\/]Scripts[\\/]python\.exe$/);
  assert.deepEqual(spec.args, [
    "-m",
    "uvicorn",
    "app.main:app",
    "--host",
    "127.0.0.1",
    "--port",
    "8123",
  ]);
  assert.equal(spec.cwd, "C:\\workspace\\aegiscopilot\\backend");
});

test("packaged launch spec points to the bundled backend executable", () => {
  const spec = buildBackendLaunchSpec({
    projectRoot: "C:\\workspace\\aegiscopilot",
    packaged: true,
    resourcesPath: "C:\\Program Files\\AegisCopilot\\resources",
    port: 8124,
  });

  assert.equal(
    spec.command,
    "C:\\Program Files\\AegisCopilot\\resources\\backend\\aegis-backend.exe",
  );
  assert.deepEqual(spec.args, ["--host", "127.0.0.1", "--port", "8124"]);
});

test("runtime environment isolates packaged data from the repository", () => {
  const env = buildRuntimeEnvironment({
    userDataPath: "C:\\Users\\CZX\\AppData\\Roaming\\AegisCopilot",
    resourcesPath: "C:\\Program Files\\AegisCopilot\\resources",
  });

  assert.equal(env.AEGIS_LLM_PROVIDER, "mock");
  assert.equal(
    env.AEGIS_STORAGE_DIR,
    "C:\\Users\\CZX\\AppData\\Roaming\\AegisCopilot\\storage",
  );
  assert.equal(
    env.AEGIS_KNOWLEDGE_DIR,
    "C:\\Program Files\\AegisCopilot\\resources\\knowledge",
  );
  assert.equal(
    env.AEGIS_MODEL_DIR,
    "C:\\Program Files\\AegisCopilot\\resources\\models\\cache",
  );
  assert.equal(env.AEGIS_EMBEDDING_LOCAL_ONLY, "1");
  assert.equal(env.AEGIS_ALLOWED_ORIGINS, "aegis://app");
  assert.equal(env.AEGIS_STORAGE_DIR.includes("F:\\agent development"), false);
});

test("extractLocalPort ignores Vite ANSI formatting", () => {
  const output = "Local:   \u001b[36mhttp://127.0.0.1:\u001b[1m5173\u001b[22m/\u001b[39m";

  assert.equal(extractLocalPort(output), 5173);
});

test("development Vite launch spec uses the direct Node CLI", () => {
  const spec = buildViteLaunchSpec({ projectRoot: "C:\\workspace\\aegiscopilot" });

  assert.equal(spec.command, "node");
  assert.equal(spec.cwd, "C:\\workspace\\aegiscopilot\\frontend");
  assert.deepEqual(spec.args, [
    "C:\\workspace\\aegiscopilot\\frontend\\node_modules\\vite\\bin\\vite.js",
    "--host",
    "127.0.0.1",
    "--port",
    "0",
  ]);
});
