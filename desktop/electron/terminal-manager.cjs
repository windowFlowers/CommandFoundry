const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");

const DEFAULT_MAX_SESSIONS = 4;
const SHELLS = Object.freeze({
  powershell: { executable: "powershell.exe", args: ["-NoLogo", "-NoProfile"] },
  cmd: { executable: "cmd.exe", args: ["/Q"] },
});

function normalizeShell(value) {
  const normalized = String(value || "powershell").trim().toLowerCase();
  if (normalized === "powershell.exe" || normalized === "pwsh") return "powershell";
  if (normalized === "cmd.exe") return "cmd";
  return normalized;
}

function safeDimension(value, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number >= 1 && number <= 5000 ? number : fallback;
}

/**
 * Owns short-lived PTY sessions for the desktop process.
 *
 * The renderer never chooses an executable.  It may choose only one of the
 * two named shells in SHELLS; all sessions disappear when the app closes.
 */
class TerminalManager extends EventEmitter {
  constructor({ ptyModule, homeDir = os.homedir(), maxSessions = DEFAULT_MAX_SESSIONS } = {}) {
    super();
    this.ptyModule = ptyModule || require("node-pty");
    this.homeDir = homeDir;
    this.maxSessions = maxSessions;
    this.sessions = new Map();
    this.nextId = 1;
  }

  create({ shell = "powershell", cwd } = {}) {
    const normalizedShell = normalizeShell(shell);
    const definition = SHELLS[normalizedShell];
    if (!definition) throw new Error("仅支持 PowerShell 或 CMD 终端。");
    if (this.sessions.size >= this.maxSessions) throw new Error(`最多同时打开 ${this.maxSessions} 个终端。`);
    const requestedCwd = typeof cwd === "string" && cwd.trim() ? cwd.trim() : this.homeDir;
    if (requestedCwd.includes("\0") || requestedCwd.includes("\r") || requestedCwd.includes("\n")) {
      throw new Error("终端工作目录无效。");
    }
    const terminalId = `terminal-${this.nextId++}`;
    const pty = this.ptyModule.spawn(definition.executable, definition.args, {
      name: "xterm-256color",
      cols: 100,
      rows: 30,
      cwd: path.resolve(requestedCwd),
      env: { ...process.env, TERM: "xterm-256color" },
      useConpty: true,
    });
    const session = { terminalId, shell: normalizedShell, cwd: path.resolve(requestedCwd), pty };
    this.sessions.set(terminalId, session);
    pty.onData?.((data) => this.emit("data", { terminalId, data }));
    pty.onExit?.((event) => {
      this.sessions.delete(terminalId);
      this.emit("exit", { terminalId, exitCode: event?.exitCode ?? 0, signal: event?.signal ?? 0 });
    });
    pty.onError?.((error) => this.emit("error", { terminalId, message: error?.message || String(error) }));
    return this.describe(session);
  }

  describe(session) {
    return { terminalId: session.terminalId, shell: session.shell, cwd: session.cwd };
  }

  write({ terminalId, data } = {}) {
    const session = this.sessions.get(String(terminalId || ""));
    if (!session) throw new Error("终端会话不存在或已关闭。");
    if (typeof data !== "string") throw new Error("终端输入必须是文本。");
    session.pty.write(data);
    return { ok: true };
  }

  resize({ terminalId, cols, rows } = {}) {
    const session = this.sessions.get(String(terminalId || ""));
    if (!session) throw new Error("终端会话不存在或已关闭。");
    session.pty.resize(safeDimension(cols, 100), safeDimension(rows, 30));
    return { ok: true };
  }

  close({ terminalId } = {}) {
    const id = String(terminalId || "");
    const session = this.sessions.get(id);
    if (!session) return { ok: false };
    this.sessions.delete(id);
    try { session.pty.kill(); } catch { /* process may already have exited */ }
    return { ok: true };
  }

  list() {
    return [...this.sessions.values()].map((session) => this.describe(session));
  }

  closeAll() {
    for (const terminalId of [...this.sessions.keys()]) this.close({ terminalId });
  }
}

module.exports = { DEFAULT_MAX_SESSIONS, SHELLS, TerminalManager, normalizeShell };
