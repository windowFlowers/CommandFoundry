const net = require("node:net");
const path = require("node:path");
const { spawn } = require("node:child_process");

function findFreePort(host = "127.0.0.1") {
  return new Promise((resolve, reject) => {
    const server = net.createServer();

    server.once("error", reject);
    server.listen({ host, port: 0 }, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close((closeError) => {
        if (closeError) {
          reject(closeError);
          return;
        }
        if (!port) {
          reject(new Error("Unable to determine a free loopback port"));
          return;
        }
        resolve(port);
      });
    });
  });
}

function extractLocalPort(output) {
  const cleanOutput = String(output || "").replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "");
  const match = cleanOutput.match(/https?:\/\/(?:127\.0\.0\.1|localhost):(\d+)/);
  return match ? Number(match[1]) : null;
}

function buildBackendLaunchSpec({
  projectRoot,
  packaged,
  resourcesPath,
  port,
  host = "127.0.0.1",
}) {
  if (!projectRoot || !port) {
    throw new Error("projectRoot and port are required to start the backend");
  }

  if (packaged) {
    if (!resourcesPath) {
      throw new Error("resourcesPath is required for packaged backend startup");
    }
    const command = path.join(resourcesPath, "backend", "aegis-backend.exe");
    return {
      command,
      args: ["--host", host, "--port", String(port)],
      cwd: path.dirname(command),
    };
  }

  const backendRoot = path.join(projectRoot, "backend");
  return {
    command: path.join(backendRoot, ".venv", "Scripts", "python.exe"),
    args: ["-m", "uvicorn", "app.main:app", "--host", host, "--port", String(port)],
    cwd: backendRoot,
  };
}

function buildViteLaunchSpec({ projectRoot, host = "127.0.0.1" }) {
  if (!projectRoot) {
    throw new Error("projectRoot is required to start Vite");
  }
  const frontendRoot = path.join(projectRoot, "frontend");
  return {
    command: "node",
    args: [
      path.join(frontendRoot, "node_modules", "vite", "bin", "vite.js"),
      "--host",
      host,
      "--port",
      "0",
    ],
    cwd: frontendRoot,
  };
}

function buildRuntimeEnvironment({ userDataPath, resourcesPath, baseEnv = {} }) {
  if (!userDataPath || !resourcesPath) {
    throw new Error("userDataPath and resourcesPath are required");
  }

  const storageDir = path.join(userDataPath, "storage");
  return {
    ...baseEnv,
    AEGIS_ENV: baseEnv.AEGIS_ENV || "local",
    AEGIS_LLM_PROVIDER: baseEnv.AEGIS_LLM_PROVIDER || "mock",
    AEGIS_STORAGE_DIR: storageDir,
    AEGIS_KNOWLEDGE_DIR: path.join(resourcesPath, "knowledge"),
    AEGIS_MODEL_DIR: path.join(resourcesPath, "models", "cache"),
    AEGIS_EMBEDDING_LOCAL_ONLY: "1",
    AEGIS_ALLOWED_ORIGINS: "aegis://app",
  };
}

async function waitForHealth(
  url,
  { timeoutMs = 60_000, intervalMs = 500, fetchImpl = globalThis.fetch } = {},
) {
  if (typeof fetchImpl !== "function") {
    throw new Error("A fetch implementation is required for health checks");
  }

  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetchImpl(url);
      if (response.ok) {
        return response;
      }
      lastError = new Error(`Backend health returned HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }

  throw new Error(`Backend did not become ready within ${timeoutMs}ms: ${lastError?.message || "unknown error"}`);
}

function stopProcessTree(child, { platform = process.platform } = {}) {
  if (!child || !child.pid || child.exitCode !== null) {
    return Promise.resolve();
  }

  if (platform !== "win32") {
    child.kill("SIGTERM");
    return Promise.resolve();
  }

  return new Promise((resolve) => {
    const taskkill = spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], {
      windowsHide: true,
      stdio: "ignore",
    });
    taskkill.once("close", () => resolve());
    taskkill.once("error", () => resolve());
  });
}

module.exports = {
  buildBackendLaunchSpec,
  buildRuntimeEnvironment,
  buildViteLaunchSpec,
  extractLocalPort,
  findFreePort,
  stopProcessTree,
  waitForHealth,
};
