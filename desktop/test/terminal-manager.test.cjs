const assert = require("node:assert/strict");
const test = require("node:test");

const { TerminalManager, normalizeShell } = require("../electron/terminal-manager.cjs");

function fakePtyModule() {
  const spawned = [];
  return {
    spawned,
    spawn(executable, args, options) {
      const handlers = {};
      const pty = {
        executable,
        args,
        options,
        writes: [],
        resizes: [],
        killed: false,
        onData(handler) { handlers.data = handler; },
        onExit(handler) { handlers.exit = handler; },
        onError(handler) { handlers.error = handler; },
        write(value) { this.writes.push(value); },
        resize(cols, rows) { this.resizes.push([cols, rows]); },
        kill() { this.killed = true; handlers.exit?.({ exitCode: 0, signal: 0 }); },
        emitData(value) { handlers.data?.(value); },
        emitError(error) { handlers.error?.(error); },
        emitExit(event) { handlers.exit?.(event); },
      };
      spawned.push(pty);
      return pty;
    },
  };
}

test("terminal manager allowlists shells and forwards PTY data", () => {
  const fake = fakePtyModule();
  const manager = new TerminalManager({ ptyModule: fake, homeDir: "C:\\Users\\tester" });
  assert.equal(normalizeShell("powershell.exe"), "powershell");
  assert.equal(normalizeShell("cmd"), "cmd");
  assert.throws(() => manager.create({ shell: "bash" }), /仅支持/);
  const events = [];
  manager.on("data", (payload) => events.push(payload));
  const session = manager.create({ shell: "cmd" });
  assert.equal(session.shell, "cmd");
  assert.equal(fake.spawned[0].executable, "cmd.exe");
  assert.deepEqual(fake.spawned[0].args, ["/Q"]);
  fake.spawned[0].emitData("hello");
  assert.deepEqual(events, [{ terminalId: session.terminalId, data: "hello" }]);
  manager.write({ terminalId: session.terminalId, data: "echo ok\r" });
  manager.resize({ terminalId: session.terminalId, cols: 120, rows: 40 });
  assert.deepEqual(fake.spawned[0].writes, ["echo ok\r"]);
  assert.deepEqual(fake.spawned[0].resizes, [[120, 40]]);
});

test("terminal manager enforces four temporary sessions and closes all PTYs", () => {
  const fake = fakePtyModule();
  const manager = new TerminalManager({ ptyModule: fake, maxSessions: 2 });
  const first = manager.create({ shell: "powershell" });
  manager.create({ shell: "cmd" });
  assert.throws(() => manager.create({ shell: "cmd" }), /最多同时打开 2 个终端/);
  assert.equal(manager.list().length, 2);
  manager.close({ terminalId: first.terminalId });
  assert.equal(manager.list().length, 1);
  manager.closeAll();
  assert.equal(manager.list().length, 0);
  assert.equal(fake.spawned.every((pty) => pty.killed), true);
});

test("terminal manager rejects malformed input and never persists session state", () => {
  const fake = fakePtyModule();
  const manager = new TerminalManager({ ptyModule: fake });
  const session = manager.create({ shell: "powershell", cwd: "C:\\workspace" });
  assert.throws(() => manager.write({ terminalId: session.terminalId, data: 5 }), /必须是文本/);
  assert.throws(() => manager.resize({ terminalId: "missing", cols: 80, rows: 24 }), /不存在/);
  assert.equal(Object.keys(manager).includes("history"), false);
  assert.equal(Object.keys(manager).includes("storage"), false);
});
