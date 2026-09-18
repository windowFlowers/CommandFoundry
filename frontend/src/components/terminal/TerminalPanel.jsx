import { useEffect, useRef, useState } from "react";
import { ChevronDown, Maximize2, Minimize2, Plus, RotateCcw, Terminal as TerminalIcon, X } from "lucide-react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

const desktopTerminal = () => window.aegisDesktop?.terminal;

function shellLabel(shell) {
  return shell === "cmd" ? "CMD" : "PowerShell";
}

export function TerminalPanel({
  open,
  onClose,
  width = 520,
  minWidth = 360,
  maxWidth = 760,
  onWidthChange,
}) {
  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [shell, setShell] = useState("powershell");
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState("");
  const [resizing, setResizing] = useState(false);
  const containers = useRef(new Map());
  const instances = useRef(new Map());
  const normalWidth = useRef(width);

  function attachSession(session) {
    if (!session || instances.current.has(session.terminalId)) return;
    const container = containers.current.get(session.terminalId);
    if (!container) return;
    const fit = new FitAddon();
    const terminal = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: "JetBrains Mono, Consolas, monospace",
      fontSize: 13,
      theme: { background: "#1d2323", foreground: "#edf0ec", cursor: "#cdd5ce" },
    });
    terminal.loadAddon(fit);
    terminal.open(container);
    fit.fit();
    terminal.onData((data) => desktopTerminal()?.write({ terminalId: session.terminalId, data }).catch?.(() => {}));
    instances.current.set(session.terminalId, { terminal, fit });
    window.setTimeout(() => {
      fit.fit();
      desktopTerminal()?.resize({ terminalId: session.terminalId, cols: terminal.cols, rows: terminal.rows }).catch?.(() => {});
    }, 0);
  }

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    desktopTerminal()?.list().then(async (items) => {
      if (cancelled) return;
      if (items.length) {
        setSessions(items);
        setActiveId(items[0].terminalId);
        return;
      }
      try {
        const created = await desktopTerminal()?.create({ shell: "powershell" });
        if (!cancelled && created) {
          setSessions([created]);
          setActiveId(created.terminalId);
        }
      } catch (loadError) {
        if (!cancelled) setError(loadError.message || "无法创建终端");
      }
    }).catch((loadError) => { if (!cancelled) setError(loadError.message || "无法读取终端会话"); });
    return () => { cancelled = true; };
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const api = desktopTerminal();
    if (!api?.on) return undefined;
    const offData = api.on("terminal:data", ({ terminalId, data }) => instances.current.get(terminalId)?.terminal.write(data));
    const offExit = api.on("terminal:exit", ({ terminalId }) => {
      instances.current.get(terminalId)?.terminal.dispose();
      instances.current.delete(terminalId);
      setSessions((current) => {
        const remaining = current.filter((item) => item.terminalId !== terminalId);
        setActiveId((active) => active === terminalId ? remaining[0]?.terminalId || null : active);
        return remaining;
      });
    });
    const offError = api.on("terminal:error", ({ message }) => setError(message || "终端发生错误"));
    return () => { offData?.(); offExit?.(); offError?.(); };
  }, [open]);

  useEffect(() => {
    sessions.forEach(attachSession);
    const active = instances.current.get(activeId);
    if (active) window.setTimeout(() => active.fit.fit(), 0);
  }, [sessions, activeId]);

  useEffect(() => () => {
    for (const { terminal } of instances.current.values()) terminal.dispose();
    instances.current.clear();
  }, []);

  async function createSession(nextShell = shell) {
    setError("");
    try {
      const created = await desktopTerminal()?.create({ shell: nextShell });
      if (!created) throw new Error("桌面终端接口不可用");
      setSessions((current) => [...current, created]);
      setActiveId(created.terminalId);
      setShell(nextShell);
    } catch (createError) { setError(createError.message || "无法创建终端"); }
  }

  async function closeSession(terminalId) {
    await desktopTerminal()?.close({ terminalId }).catch?.(() => {});
    instances.current.get(terminalId)?.terminal.dispose();
    instances.current.delete(terminalId);
    setSessions((current) => {
      const remaining = current.filter((item) => item.terminalId !== terminalId);
      setActiveId((active) => active === terminalId ? remaining[0]?.terminalId || null : active);
      return remaining;
    });
  }

  function clearActive() { instances.current.get(activeId)?.terminal.clear(); }

  function beginResize(event) {
    if (event.button !== 0 || typeof onWidthChange !== "function") return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = width;
    setResizing(true);
    const move = (moveEvent) => onWidthChange(startWidth + startX - moveEvent.clientX);
    const end = () => {
      setResizing(false);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end, { once: true });
  }

  function resizeWithKeyboard(event) {
    if (typeof onWidthChange !== "function") return;
    const step = event.shiftKey ? 80 : 24;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      onWidthChange(width + step);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      onWidthChange(width - step);
    } else if (event.key === "Home") {
      event.preventDefault();
      onWidthChange(minWidth);
    } else if (event.key === "End") {
      event.preventDefault();
      onWidthChange(maxWidth);
    }
  }

  function toggleExpanded() {
    if (expanded) {
      onWidthChange(normalWidth.current);
    } else {
      normalWidth.current = width;
      onWidthChange(Math.max(width, Math.min(maxWidth, 680)));
    }
    setExpanded((value) => !value);
  }

  if (!open) return null;
  return (
    <section className={`terminal-panel ${expanded ? "expanded" : ""} ${resizing ? "resizing" : ""}`} data-ui="terminal-panel" aria-label="内置终端" style={{ "--terminal-width": `${width}px` }}>
      <div
        className="terminal-panel-resize-handle"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整终端宽度"
        aria-valuemin={minWidth}
        aria-valuemax={maxWidth}
        aria-valuenow={width}
        tabIndex={0}
        onPointerDown={beginResize}
        onKeyDown={resizeWithKeyboard}
      />
      <header className="terminal-panel-head">
        <div className="terminal-panel-title"><TerminalIcon size={16} /><strong>终端</strong><span>{sessions.length} / 4</span></div>
        <div className="terminal-panel-actions">
          <button type="button" onClick={() => createSession("powershell")} disabled={sessions.length >= 4} aria-label="新建 PowerShell 终端"><Plus size={15} />PowerShell</button>
          <button type="button" onClick={() => createSession("cmd")} disabled={sessions.length >= 4} aria-label="新建 CMD 终端"><Plus size={15} />CMD</button>
          <button type="button" onClick={clearActive} disabled={!activeId} aria-label="清屏"><RotateCcw size={15} /></button>
          <button type="button" onClick={toggleExpanded} aria-label={expanded ? "还原终端宽度" : "放大终端"}>{expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
          <button type="button" onClick={onClose} aria-label="关闭终端"><X size={16} /></button>
        </div>
      </header>
      <div className="terminal-session-tabs" role="tablist" aria-label="终端会话">
        {sessions.map((session) => (
          <button key={session.terminalId} type="button" role="tab" aria-selected={session.terminalId === activeId} className={session.terminalId === activeId ? "active" : ""} onClick={() => setActiveId(session.terminalId)}>
            <span>{shellLabel(session.shell)}</span><small>{session.terminalId.replace("terminal-", "#")}</small><X size={13} onClick={(event) => { event.stopPropagation(); closeSession(session.terminalId); }} aria-label={`关闭 ${shellLabel(session.shell)} ${session.terminalId}`} />
          </button>
        ))}
        {sessions.length === 0 && <span className="terminal-empty">没有活动终端</span>}
      </div>
      <div className="terminal-views">
        {sessions.map((session) => <div key={session.terminalId} ref={(node) => { if (node) containers.current.set(session.terminalId, node); else containers.current.delete(session.terminalId); }} className={`terminal-view ${session.terminalId === activeId ? "active" : ""}`} role="tabpanel" aria-label={`${shellLabel(session.shell)} 终端`} />)}
      </div>
      {error && <p className="terminal-error" role="alert">{error}</p>}
      <div className="terminal-panel-foot"><span>仅允许 PowerShell / CMD · 输出不会保存到应用数据库</span><span><ChevronDown size={13} />用户确认后才执行助手命令</span></div>
    </section>
  );
}
