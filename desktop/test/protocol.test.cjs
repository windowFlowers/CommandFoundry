const test = require("node:test");
const assert = require("node:assert/strict");

const { resolveProtocolPath } = require("../electron/protocol.cjs");

test("protocol serves an existing asset inside the frontend dist directory", () => {
  const resolved = resolveProtocolPath("C:\\app\\frontend", "/assets/index.js", {
    existsSync: (candidate) => candidate === "C:\\app\\frontend\\assets\\index.js",
  });

  assert.equal(resolved, "C:\\app\\frontend\\assets\\index.js");
});

test("protocol falls back to index.html for browser routes", () => {
  const resolved = resolveProtocolPath("C:\\app\\frontend", "/admin/knowledge", {
    existsSync: (candidate) => candidate === "C:\\app\\frontend\\index.html",
  });

  assert.equal(resolved, "C:\\app\\frontend\\index.html");
});

test("protocol rejects paths that escape the frontend dist directory", () => {
  const resolved = resolveProtocolPath("C:\\app\\frontend", "/..\\..\\secrets.txt", {
    existsSync: () => true,
  });

  assert.equal(resolved, "C:\\app\\frontend\\index.html");
});
