import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  Box,
  Check,
  ChevronDown,
  Clipboard,
  Code2,
  Database,
  FileCode2,
  GitBranch,
  Globe2,
  LoaderCircle,
  Menu,
  MessageSquareText,
  PanelLeftClose,
  Plus,
  Send,
  Settings,
  Shell,
  Trash2,
  WifiOff,
  X,
} from "lucide-react";
import { fetchJson, streamChat } from "./lib/api";


const examples = [
  { id: "linux", label: "Linux / Shell", question: "在 Linux 中切换文件目录的指令是什么？", icon: Shell },
  { id: "git", label: "Git", question: "Git 怎么安全回滚一次提交？", icon: GitBranch },
  { id: "sql", label: "SQL / MySQL", question: "MySQL 中怎么创建一个视图？", icon: Database },
  { id: "docker", label: "Docker", question: "Docker 怎么查看容器最近 100 行日志？", icon: Box },
  { id: "http", label: "HTTP / cURL", question: "如何用 cURL 发送带 JSON 的 POST 请求？", icon: Globe2 },
  { id: "toolchain", label: "Python / Node", question: "Python 怎么创建并激活虚拟环境？", icon: FileCode2 },
];

const riskText = { low: "低风险", medium: "需留意", high: "高风险" };
const domainText = {
  linux: "Linux / Shell",
  git: "Git",
  sql: "SQL / MySQL",
  docker: "Docker",
  http: "HTTP / cURL",
  toolchain: "Python / Node",
};

function CopyButton({ value }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }
  return (
    <button className="copy-button" type="button" onClick={copy} aria-label="复制命令">
      {copied ? <Check size={15} /> : <Clipboard size={15} />}
      <span>{copied ? "已复制" : "复制"}</span>
    </button>
  );
}

function CommandCard({ command, index }) {
  return (
    <section className={`command-card risk-${command.risk}`}>
      <header className="command-head">
        <div>
          <span className="command-index">{String(index + 1).padStart(2, "0")}</span>
          <strong>{command.label}</strong>
        </div>
        <span className={`risk-badge ${command.risk}`}>{riskText[command.risk] || command.risk}</span>
      </header>
      <div className="code-shell">
        <div className="code-toolbar">
          <span>{command.language}</span>
          <CopyButton value={command.code} />
        </div>
        <pre><code>{command.code}</code></pre>
      </div>
      <div className="command-meta">
        {command.platforms?.length > 0 && (
          <div><span className="meta-label">运行平台</span><span>{command.platforms.join(" · ")}</span></div>
        )}
        {command.prerequisites?.length > 0 && (
          <div><span className="meta-label">执行前</span><span>{command.prerequisites.join("；")}</span></div>
        )}
        {command.warning && (
          <div className="warning-row"><AlertTriangle size={15} /><span>{command.warning}</span></div>
        )}
      </div>
    </section>
  );
}

function AnswerCard({ answer }) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  return (
    <article className="answer-card">
      <div className="answer-topline">
        <span className="assistant-mark"><Code2 size={17} /></span>
        <span>AegisCopilot</span>
        <span className={`mode-badge ${answer.mode}`}>
          {answer.mode === "model" ? "RAG + DeepSeek" : "本地检索回答"}
        </span>
      </div>
      <p className="answer-summary">{answer.summary}</p>
      <div className="commands-list">
        {answer.commands?.map((command, index) => (
          <CommandCard key={`${command.label}-${index}`} command={command} index={index} />
        ))}
      </div>
      {answer.notes?.length > 0 && (
        <ul className="notes-list">
          {answer.notes.map((note, index) => <li key={`${note}-${index}`}>{note}</li>)}
        </ul>
      )}
      {answer.citations?.length > 0 && (
        <div className="citations">
          <button className="citations-toggle" type="button" onClick={() => setSourcesOpen((value) => !value)}>
            <BookOpen size={16} />
            <span>{answer.citations.length} 个可追溯来源</span>
            <ChevronDown className={sourcesOpen ? "rotate" : ""} size={16} />
          </button>
          {sourcesOpen && (
            <div className="citation-list">
              {answer.citations.map((citation) => (
                <a key={citation.source_id} href={citation.source_url} target="_blank" rel="noreferrer">
                  <span className="citation-domain">{domainText[citation.domain] || citation.domain}</span>
                  <strong>{citation.title}</strong>
                  <small>{citation.license} · {citation.revision.slice(0, 9)}</small>
                  <p>{citation.excerpt}</p>
                </a>
              ))}
            </div>
          )}
        </div>
      )}
    </article>
  );
}

function EmptyState({ onExample }) {
  return (
    <div className="empty-state">
      <div className="empty-copy">
        <span className="eyebrow">LOCAL-FIRST COMMAND RAG</span>
        <h1>把问题变成<br />可以直接使用的命令</h1>
        <p>从本地知识库检索可靠用法，再由模型整理上下文。每条命令都会标注平台、前置条件、风险和来源。</p>
      </div>
      <div className="example-grid" aria-label="示例问题">
        {examples.map(({ id, label, question, icon: Icon }) => (
          <button key={id} type="button" onClick={() => onExample(question)}>
            <span className="example-icon"><Icon size={18} /></span>
            <span><strong>{label}</strong><small>{question}</small></span>
          </button>
        ))}
      </div>
    </div>
  );
}

function SettingsDrawer({ open, onClose, modelStatus, onChanged }) {
  const [key, setKey] = useState("");
  const [message, setMessage] = useState("");
  const [working, setWorking] = useState(false);
  const desktopApi = window.aegisDesktop?.modelConfig;

  useEffect(() => {
    if (open) setMessage("");
  }, [open]);

  async function run(action) {
    if (!desktopApi) {
      setMessage("模型配置仅在 Electron 桌面端可用。");
      return;
    }
    setWorking(true);
    setMessage("");
    try {
      if (action === "save") {
        if (!key.trim()) throw new Error("请输入 API Key");
        await desktopApi.save({ apiKey: key.trim() });
        setKey("");
        setMessage("已加密保存，后端正在安全重启。");
      } else if (action === "test") {
        const result = await desktopApi.test({ apiKey: key.trim() || undefined });
        setMessage(result.message || "连接成功。");
      } else {
        await desktopApi.clear();
        setKey("");
        setMessage("已清除本机保存的密钥。");
      }
      await onChanged();
    } catch (error) {
      setMessage(error.message || "操作失败");
    } finally {
      setWorking(false);
    }
  }

  return (
    <>
      <button className={`drawer-scrim ${open ? "visible" : ""}`} type="button" onClick={onClose} aria-label="关闭设置" />
      <aside className={`settings-drawer ${open ? "open" : ""}`} aria-hidden={!open}>
        <header><div><span className="eyebrow">MODEL SETTINGS</span><h2>DeepSeek 配置</h2></div><button className="icon-button" type="button" onClick={onClose}><X size={19} /></button></header>
        <p className="drawer-intro">密钥由 Electron safeStorage 调用 Windows DPAPI 加密，渲染进程无法读取已经保存的内容。</p>
        <div className="model-state">
          <span className={`status-dot ${modelStatus?.configured ? "ready" : ""}`} />
          <div><strong>{modelStatus?.configured ? "模型已配置" : "当前使用本地回答"}</strong><small>deepseek-chat · OpenAI-compatible</small></div>
        </div>
        <label className="field-label" htmlFor="api-key">DeepSeek API Key</label>
        <input id="api-key" className="text-input" type="password" value={key} onChange={(event) => setKey(event.target.value)} placeholder="仅在本机加密保存" autoComplete="off" />
        <p className="field-help">保存后应用会重启本地后端。公开演示前请使用专门的演示密钥。</p>
        {message && <p className="settings-message" role="status">{message}</p>}
        <div className="drawer-actions">
          <button className="secondary-button" type="button" disabled={working} onClick={() => run("test")}>测试连接</button>
          <button className="primary-button" type="button" disabled={working || !key.trim()} onClick={() => run("save")}>{working ? "处理中" : "加密保存"}</button>
        </div>
        {modelStatus?.configured && <button className="clear-key" type="button" disabled={working} onClick={() => run("clear")}>清除已保存密钥</button>}
      </aside>
    </>
  );
}

export function App() {
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [query, setQuery] = useState("");
  const [knowledge, setKnowledge] = useState(null);
  const [modelStatus, setModelStatus] = useState({ configured: false });
  const [statusText, setStatusText] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 900);
  const composerRef = useRef(null);
  const bottomRef = useRef(null);

  async function loadModelStatus() {
    try {
      const status = await window.aegisDesktop?.modelConfig?.status?.();
      if (status) setModelStatus(status);
    } catch {
      setModelStatus({ configured: false });
    }
  }

  async function refreshConversations() {
    const payload = await fetchJson("/conversations");
    setConversations(payload.items);
  }

  useEffect(() => {
    Promise.all([fetchJson("/knowledge/status"), fetchJson("/conversations"), loadModelStatus()])
      .then(([knowledgePayload, conversationPayload]) => {
        setKnowledge(knowledgePayload);
        setConversations(conversationPayload.items);
      })
      .catch((loadError) => setError(loadError.message));
    const shortcut = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        composerRef.current?.focus();
      }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);

  useEffect(() => {
    const lastMessage = messages.at(-1);
    const target = lastMessage?.role === "assistant"
      ? document.querySelector(".answer-card:last-of-type")
      : bottomRef.current;
    target?.scrollIntoView({ behavior: "smooth", block: lastMessage?.role === "assistant" ? "start" : "end" });
  }, [messages, statusText]);

  const activeConversation = useMemo(
    () => conversations.find((item) => item.id === activeId),
    [activeId, conversations],
  );

  async function selectConversation(id) {
    if (busy) return;
    setError("");
    const conversation = await fetchJson(`/conversations/${id}`);
    setActiveId(id);
    setMessages(conversation.messages);
  }

  async function deleteConversation(event, id) {
    event.stopPropagation();
    await fetchJson(`/conversations/${id}`, { method: "DELETE" });
    if (activeId === id) {
      setActiveId(null);
      setMessages([]);
    }
    await refreshConversations();
  }

  function newConversation() {
    if (busy) return;
    setActiveId(null);
    setMessages([]);
    setError("");
    window.setTimeout(() => composerRef.current?.focus(), 0);
  }

  async function submit(rawQuery = query) {
    const value = rawQuery.trim();
    if (!value || busy) return;
    setQuery("");
    setError("");
    setBusy(true);
    setMessages((current) => [...current, { id: `local-${Date.now()}`, role: "user", query: value }]);
    try {
      await streamChat({
        query: value,
        conversationId: activeId,
        onEvent: ({ event, data }) => {
          if (event === "status") setStatusText(data.message);
          if (event === "answer") {
            setActiveId(data.conversation_id);
            setMessages((current) => [...current, { id: data.message_id, role: "assistant", answer: data.answer }]);
          }
          if (event === "error") throw new Error(data.message);
        },
      });
      await refreshConversations();
    } catch (submitError) {
      setError(submitError.message || "暂时无法生成回答");
    } finally {
      setBusy(false);
      setStatusText("");
    }
  }

  return (
    <div className="app-frame">
      <div className="titlebar">
        <div className="titlebar-brand"><img src="./app-icon.png" alt="" /><span>AegisCopilot</span><small>v2</small></div>
        <span className="titlebar-context">Developer Command RAG</span>
      </div>
      <div className="workspace">
        <aside className={`sidebar ${sidebarOpen ? "" : "collapsed"}`}>
          <div className="sidebar-head">
            <button className="new-chat" type="button" onClick={newConversation}><Plus size={17} /><span>新建对话</span></button>
            <button className="icon-button" type="button" onClick={() => setSidebarOpen(false)} aria-label="收起侧栏"><PanelLeftClose size={18} /></button>
          </div>
          <div className="session-label">最近对话</div>
          <nav className="session-list" aria-label="最近对话">
            {conversations.length === 0 && <p className="no-sessions">问题会保存在这台设备上</p>}
            {conversations.map((conversation) => (
              <button key={conversation.id} className={conversation.id === activeId ? "active" : ""} type="button" onClick={() => selectConversation(conversation.id)}>
                <MessageSquareText size={15} />
                <span>{conversation.title}</span>
                <span className="delete-session" role="button" tabIndex={0} onClick={(event) => deleteConversation(event, conversation.id)}><Trash2 size={14} /></span>
              </button>
            ))}
          </nav>
          <div className="sidebar-status">
            <div className="knowledge-row"><span className={`status-dot ${knowledge?.ready ? "ready" : ""}`} /><div><strong>{knowledge?.topic_count || 0} 个知识主题</strong><small>{knowledge?.retrieval_mode === "hybrid" ? "BM25 + 向量 · RRF" : "BM25 本地检索"}</small></div></div>
            <button type="button" onClick={() => setDrawerOpen(true)}><Settings size={16} /><span>模型与隐私</span><small>{modelStatus.configured ? "已配置" : "本地模式"}</small></button>
          </div>
        </aside>
        <main className="chat-panel">
          <header className="chat-header">
            <div className="chat-title-wrap">
              {!sidebarOpen && <button className="icon-button" type="button" onClick={() => setSidebarOpen(true)} aria-label="展开侧栏"><Menu size={19} /></button>}
              <div><span className="eyebrow">COMMAND WORKSPACE</span><h2>{activeConversation?.title || "新的技术问题"}</h2></div>
            </div>
            <div className="header-state"><span className="status-dot ready" />本地知识已就绪</div>
          </header>
          <div className="message-viewport">
            {messages.length === 0 ? <EmptyState onExample={submit} /> : (
              <div className="message-column">
                {messages.map((message) => message.role === "user" ? (
                  <div key={message.id} className="user-message"><span>你</span><p>{message.query}</p></div>
                ) : <AnswerCard key={message.id} answer={message.answer} />)}
                {busy && <div className="thinking"><LoaderCircle size={17} /><span>{statusText || "正在准备回答"}</span></div>}
                {error && <div className="error-banner"><WifiOff size={17} /><span>{error}</span></div>}
                <div ref={bottomRef} />
              </div>
            )}
          </div>
          <div className="composer-wrap">
            {error && messages.length === 0 && <div className="error-banner"><WifiOff size={17} /><span>{error}</span></div>}
            <div className="composer">
              <textarea ref={composerRef} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => {
                if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submit(); }
              }} placeholder="描述你要完成的开发任务，例如：Git 怎么安全回滚一次提交？" rows={1} disabled={busy} />
              <button type="button" onClick={() => submit()} disabled={busy || !query.trim()} aria-label="发送问题"><Send size={18} /></button>
            </div>
            <div className="composer-foot"><span>Ctrl + Enter 发送</span><span>命令仅供复制，不会自动执行</span></div>
          </div>
        </main>
      </div>
      <SettingsDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} modelStatus={modelStatus} onChanged={loadModelStatus} />
    </div>
  );
}
