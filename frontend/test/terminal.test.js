import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const terminalSource = await readFile(new URL("../src/components/terminal/TerminalPanel.jsx", import.meta.url), "utf8");
const chatSource = await readFile(new URL("../src/components/chat/ChatWorkspace.jsx", import.meta.url), "utf8");
const appSource = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
const drawerSource = await readFile(new URL("../src/components/overlays/AppDrawers.jsx", import.meta.url), "utf8");

test("terminal panel uses scoped PTY IPC, caps sessions, and does not persist output", () => {
  assert.match(terminalSource, /@xterm\/xterm/);
  assert.match(terminalSource, /@xterm\/addon-fit/);
  assert.match(terminalSource, /terminal:create|create\(\{ shell/);
  assert.match(terminalSource, /terminal:data/);
  assert.match(terminalSource, /terminal:exit/);
  assert.match(terminalSource, /sessions\.length >= 4/);
  assert.match(terminalSource, /输出不会保存到应用数据库/);
  assert.doesNotMatch(terminalSource, /localStorage|sessionStorage|fetch\(/);
  assert.match(terminalSource, /terminal-panel-resize-handle/);
  assert.match(terminalSource, /aria-valuemin/);
});

test("terminal opens as a resizable side dock instead of covering the chat", () => {
  assert.match(appSource, /terminal-docked/);
  assert.match(appSource, /--terminal-width/);
  assert.match(appSource, /onWidthChange/);
});

test("model settings expose multiple OpenAI-compatible providers", () => {
  assert.match(drawerSource, /DeepSeek/);
  assert.match(drawerSource, /OpenAI/);
  assert.match(drawerSource, /通义千问/);
  assert.match(drawerSource, /Moonshot/);
  assert.match(drawerSource, /Ollama/);
  assert.match(drawerSource, /自定义 OpenAI 兼容 API/);
  assert.match(drawerSource, /API Base URL/);
});

test("assistant execution is wired only from direct command answers", () => {
  assert.match(chatSource, /answer_kind === "direct"/);
  assert.match(chatSource, /在终端执行/);
  assert.match(chatSource, /确认执行/);
  assert.match(chatSource, /risk === "high"/);
  assert.match(appSource, /executeCommand/);
  assert.match(appSource, /onExecuteCommand/);
});
