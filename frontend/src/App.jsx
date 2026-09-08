import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  Box,
  Check,
  ChevronDown,
  Clipboard,
  Code2,
  Database,
  FileCode2,
  FileText,
  FolderCog,
  GitBranch,
  Globe2,
  LoaderCircle,
  Menu,
  MessageSquareText,
  PanelLeftClose,
  Pencil,
  Plus,
  RefreshCw,
  Send,
  Settings,
  Shell,
  Trash2,
  UploadCloud,
  WifiOff,
  X,
} from "lucide-react";
import { fetchJson, streamChat } from "./lib/api";


const DEFAULT_KNOWLEDGE_BASE_ID = "developer-it";
const examples = [
  { id: "linux", label: "Linux / Shell", question: "在 Linux 中切换文件目录的指令是什么？", icon: Shell },
  { id: "git", label: "Git", question: "Git 怎么安全回滚一次提交？", icon: GitBranch },
  { id: "sql", label: "SQL / MySQL", question: "MySQL 中怎么创建一个视图？", icon: Database },
  { id: "docker", label: "Docker", question: "Docker 怎么查看容器最近 100 行日志？", icon: Box },
  { id: "http", label: "HTTP / cURL", question: "如何用 cURL 发送带 JSON 的 POST 请求？", icon: Globe2 },
  { id: "toolchain", label: "Python / Node", question: "Python 怎么创建并激活虚拟环境？", icon: FileCode2 },
];

const riskText = { low: "低风险", medium: "需留意", high: "高风险" };
const fallbackText = {
  no_api_key: "未配置 API Key",
  no_hits: "没有检索命中",
  timeout: "DeepSeek 请求超时",
  rate_limit: "DeepSeek 请求限流",
  authentication: "API Key 认证失败",
  invalid_json: "模型响应格式无效",
  provider_error: "DeepSeek 服务异常",
};
const domainText = {
  linux: "Linux / Shell", git: "Git", sql: "SQL", docker: "Docker",
  http: "HTTP / API", toolchain: "开发工具链", windows: "Windows / PowerShell",
  kubernetes: "Kubernetes", network: "网络 / Nginx", redis: "Redis",
  java: "Java", cicd: "CI / CD", testing: "测试与调试", uploaded: "上传文档",
};

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function CopyButton({ value, kind = "命令", compact = false }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }
  return (
    <button className={`copy-button ${compact ? "compact" : ""}`} type="button" onClick={copy} aria-label={`复制${kind}`}>
      {copied ? <Check size={15} /> : <Clipboard size={15} />}
      <span>{copied ? "已复制" : `复制${compact ? "" : ""}`}</span>
    </button>
  );
}

function CommandCard({ command, index }) {
  return (
    <section className={`command-card risk-${command.risk}`}>
      <header className="command-head">
        <div><span className="command-index">{String(index + 1).padStart(2, "0")}</span><strong>{command.label}</strong></div>
        <span className={`risk-badge ${command.risk}`}>{riskText[command.risk] || command.risk}</span>
      </header>
      <div className="code-shell">
        <div className="code-toolbar"><span>{command.language}</span><CopyButton value={command.code} /></div>
        <pre><code>{command.code}</code></pre>
      </div>
      <div className="command-meta">
        {command.platforms?.length > 0 && <div><span className="meta-label">运行平台</span><span>{command.platforms.join(" · ")}</span></div>}
        {command.prerequisites?.length > 0 && <div><span className="meta-label">执行前</span><span>{command.prerequisites.join("；")}</span></div>}
        {command.warning && <div className="warning-row"><AlertTriangle size={15} /><span>{command.warning}</span></div>}
      </div>
    </section>
  );
}

function GenerationBadge({ answer }) {
  const generation = answer.generation || {};
  if (answer.mode === "model") {
    const stats = [
      generation.total_tokens ? `${generation.total_tokens} token` : null,
      generation.latency_ms ? `${generation.latency_ms} ms` : null,
    ].filter(Boolean);
    return <span className="generation-badge model">DeepSeek{stats.length ? ` · ${stats.join(" · ")}` : ""}</span>;
  }
  const reason = fallbackText[generation.fallback_reason] || "本地确定性回答";
  return <span className="generation-badge local">本地回退 · {reason}</span>;
}

function AnswerCard({ answer, onPreviewDocument }) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  return (
    <article className="answer-card">
      <div className="answer-topline">
        <span className="assistant-mark"><Code2 size={17} /></span>
        <span>AegisCopilot</span>
        <GenerationBadge answer={answer} />
      </div>
      <p className="answer-summary">{answer.summary}</p>
      {answer.commands?.length > 0 && (
        <div className="commands-list">{answer.commands.map((command, index) => <CommandCard key={`${command.label}-${index}`} command={command} index={index} />)}</div>
      )}
      {answer.notes?.length > 0 && <ul className="notes-list">{answer.notes.map((note, index) => <li key={`${note}-${index}`}>{note}</li>)}</ul>}
      {answer.citations?.length > 0 && (
        <div className="citations">
          <button className="citations-toggle" type="button" onClick={() => setSourcesOpen((value) => !value)}>
            <BookOpen size={16} /><span>{answer.citations.length} 个可追溯来源</span><ChevronDown className={sourcesOpen ? "rotate" : ""} size={16} />
          </button>
          {sourcesOpen && (
            <div className="citation-list">
              {answer.citations.map((citation, index) => {
                const content = <><span className="citation-domain">{domainText[citation.domain] || citation.domain}</span><strong>{citation.title}</strong><small>{citation.license} · {citation.revision?.slice(0, 12)}</small><p>{citation.excerpt}</p></>;
                return citation.source_url ? (
                  <a key={`${citation.source_id}-${index}`} href={citation.source_url} target="_blank" rel="noreferrer">{content}</a>
                ) : (
                  <button key={`${citation.source_id}-${index}`} type="button" onClick={() => citation.document_id && onPreviewDocument(citation.document_id)}>{content}</button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </article>
  );
}

function UserMessage({ message }) {
  return (
    <div className="user-message">
      <div className="user-message-head"><span>你</span><CopyButton value={message.query} kind="问题" compact /></div>
      <p>{message.query}</p>
    </div>
  );
}

function EmptyState({ onExample }) {
  return (
    <div className="empty-state">
      <div className="empty-copy"><span className="eyebrow">LOCAL-FIRST COMMAND RAG</span><h1>把问题变成<br />可以直接使用的命令</h1><p>从所选知识库检索可靠用法，再由模型整理上下文。命令会标注平台、前置条件、风险和来源。</p></div>
      <div className="example-grid" aria-label="示例问题">
        {examples.map(({ id, label, question, icon: Icon }) => (
          <button key={id} type="button" onClick={() => onExample(question)}><span className="example-icon"><Icon size={18} /></span><span><strong>{label}</strong><small>{question}</small></span></button>
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
  useEffect(() => { if (open) setMessage(""); }, [open]);

  async function run(action) {
    if (!desktopApi) { setMessage("模型配置仅在桌面端可用"); return; }
    setWorking(true); setMessage("");
    try {
      if (action === "save") {
        if (!key.trim()) throw new Error("请输入 API Key");
        await desktopApi.save({ apiKey: key.trim() }); setKey(""); setMessage("已加密保存");
      } else if (action === "test") {
        const result = await desktopApi.test({ apiKey: key.trim() || undefined }); setMessage(result.message || "连接成功");
      } else {
        await desktopApi.clear(); setKey(""); setMessage("已清除密钥");
      }
      await onChanged();
    } catch (error) { setMessage(error.message || "操作失败"); }
    finally { setWorking(false); }
  }

  return <>
    <button className={`drawer-scrim ${open ? "visible" : ""}`} type="button" onClick={onClose} aria-label="关闭设置" />
    <aside className={`settings-drawer ${open ? "open" : ""}`} aria-hidden={!open}>
      <header><div><span className="eyebrow">MODEL SETTINGS</span><h2>DeepSeek 配置</h2></div><button className="icon-button" type="button" onClick={onClose}><X size={19} /></button></header>
      <div className="model-state compact-state"><span className={`status-dot ${modelStatus?.configured ? "ready" : ""}`} /><strong>{modelStatus?.configured ? "deepseek-chat 已配置" : "当前使用本地回答"}</strong></div>
      <label className="field-label" htmlFor="api-key">API Key</label>
      <input id="api-key" className="text-input" type="password" value={key} onChange={(event) => setKey(event.target.value)} placeholder="输入 DeepSeek API Key" autoComplete="off" />
      {message && <p className="settings-message" role="status">{message}</p>}
      <div className="drawer-actions"><button className="secondary-button" type="button" disabled={working} onClick={() => run("test")}>测试真实对话</button><button className="primary-button" type="button" disabled={working || !key.trim()} onClick={() => run("save")}>{working ? "处理中" : "保存"}</button></div>
      {modelStatus?.configured && <button className="clear-key" type="button" disabled={working} onClick={() => run("clear")}>清除已保存密钥</button>}
    </aside>
  </>;
}

function Modal({ title, children, onClose, actions }) {
  return <div className="modal-layer" role="dialog" aria-modal="true" aria-label={title}><button className="modal-scrim" type="button" onClick={onClose} aria-label="关闭" /><section className="modal-card"><header><h3>{title}</h3><button className="icon-button" type="button" onClick={onClose}><X size={18} /></button></header><div className="modal-content">{children}</div>{actions && <footer>{actions}</footer>}</section></div>;
}

function KnowledgeManager({ knowledgeBases, initialId, onRefresh, onBack, onSelectForChat }) {
  const [activeId, setActiveId] = useState(initialId || DEFAULT_KNOWLEDGE_BASE_ID);
  const [documents, setDocuments] = useState([]);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const [newName, setNewName] = useState("");
  const [renameName, setRenameName] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [preview, setPreview] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const fileRef = useRef(null);
  const activeBase = knowledgeBases.find((item) => item.id === activeId) || knowledgeBases[0];

  async function load(baseId = activeId) {
    try {
      const [docs, state] = await Promise.all([
        fetchJson(`/knowledge-bases/${baseId}/documents`),
        fetchJson(`/knowledge/status?knowledge_base_id=${encodeURIComponent(baseId)}`),
      ]);
      setDocuments(docs.items); setStatus(state); setError("");
    } catch (loadError) { setError(loadError.message); }
  }

  useEffect(() => { if (activeBase) load(activeBase.id); }, [activeId, knowledgeBases.length]);
  useEffect(() => {
    if (!documents.some((item) => item.status === "pending" || item.status === "indexing")) return undefined;
    const timer = window.setInterval(() => { load(); onRefresh(); }, 1200);
    return () => window.clearInterval(timer);
  }, [documents, activeId]);

  async function createBase(event) {
    event.preventDefault(); if (!newName.trim()) return;
    try { const created = await fetchJson("/knowledge-bases", { method: "POST", body: { name: newName.trim() } }); setNewName(""); await onRefresh(); setActiveId(created.id); }
    catch (actionError) { setError(actionError.message); }
  }

  async function renameBase(event) {
    event.preventDefault(); if (!renameName.trim()) return;
    try { await fetchJson(`/knowledge-bases/${activeId}`, { method: "PATCH", body: { name: renameName.trim() } }); setRenaming(false); setRenameName(""); await onRefresh(); }
    catch (actionError) { setError(actionError.message); }
  }

  async function upload(files) {
    const selected = Array.from(files || []); if (!selected.length) return;
    setUploading(true); setError("");
    try {
      for (const file of selected) { const form = new FormData(); form.append("file", file); await fetchJson(`/knowledge-bases/${activeId}/documents`, { method: "POST", body: form }); }
      await load(); await onRefresh();
    } catch (actionError) { setError(actionError.message); }
    finally { setUploading(false); if (fileRef.current) fileRef.current.value = ""; }
  }

  async function previewDocument(documentId) {
    try { setPreview(await fetchJson(`/knowledge-documents/${documentId}/content`)); }
    catch (actionError) { setError(actionError.message); }
  }

  async function deleteDocument(document) {
    try { await fetchJson(`/knowledge-documents/${document.id}`, { method: "DELETE" }); setDeleteTarget(null); await load(); await onRefresh(); }
    catch (actionError) { setError(actionError.message); }
  }

  async function deleteBase(base) {
    try { await fetchJson(`/knowledge-bases/${base.id}`, { method: "DELETE" }); setDeleteTarget(null); setActiveId(DEFAULT_KNOWLEDGE_BASE_ID); await onRefresh(); }
    catch (actionError) { setError(actionError.message); }
  }

  if (!activeBase) return null;
  return <main className="knowledge-page">
    <header className="knowledge-page-header"><button className="secondary-button back-button" type="button" onClick={onBack}><ArrowLeft size={16} />返回问答</button><div><span className="eyebrow">KNOWLEDGE WORKSPACE</span><h1>知识库管理</h1></div></header>
    <div className="knowledge-layout">
      <aside className="library-panel">
        <div className="panel-title"><span>知识库</span><span>{knowledgeBases.length}</span></div>
        <nav>{knowledgeBases.map((base) => <button key={base.id} className={base.id === activeId ? "active" : ""} type="button" onClick={() => setActiveId(base.id)}><BookOpen size={16} /><span><strong>{base.name}</strong><small>{base.is_builtin ? "内置" : `${base.document_count} 个文档`}</small></span></button>)}</nav>
        <form className="new-library" onSubmit={createBase}><input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="新知识库名称" maxLength={64} /><button type="submit" disabled={!newName.trim()}><Plus size={16} /></button></form>
      </aside>
      <section className="documents-panel">
        <header className="documents-head"><div>{renaming ? <form className="rename-form" onSubmit={renameBase}><input autoFocus value={renameName} onChange={(event) => setRenameName(event.target.value)} /><button type="submit">保存</button><button type="button" onClick={() => setRenaming(false)}>取消</button></form> : <><h2>{activeBase.name}</h2><div className="index-facts"><span>{status?.topic_count || 0} 个索引片段</span><span>{status?.retrieval_mode === "hybrid" ? "BM25 + BGE · RRF" : "BM25"}</span></div></>}</div><div className="document-actions"><button type="button" onClick={() => onSelectForChat(activeBase.id)}>用于新对话</button>{!activeBase.is_builtin && <><button type="button" onClick={() => { setRenameName(activeBase.name); setRenaming(true); }}><Pencil size={15} />重命名</button><button className="danger-link" type="button" onClick={() => setDeleteTarget({ type: "base", item: activeBase })}><Trash2 size={15} />删除</button></>}</div></header>
        <button className={`upload-zone ${dragging ? "dragging" : ""}`} type="button" onClick={() => fileRef.current?.click()} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); upload(event.dataTransfer.files); }}><UploadCloud size={24} /><strong>{uploading ? "正在上传" : "选择或拖入文档"}</strong><span>TXT · Markdown · PDF · DOCX · 最大 20 MB</span></button>
        <input ref={fileRef} className="file-input" type="file" multiple accept=".txt,.md,.markdown,.pdf,.docx" onChange={(event) => upload(event.target.files)} />
        {error && <div className="error-banner"><WifiOff size={17} /><span>{error}</span></div>}
        <div className="document-table"><div className="document-table-head"><span>文档</span><span>状态</span><span>大小</span><span>更新时间</span><span>操作</span></div>{documents.length === 0 ? <div className="empty-documents"><FileText size={24} /><span>还没有上传文档</span></div> : documents.map((document) => <div className="document-row" key={document.id}><div><FileText size={17} /><span><strong>{document.filename}</strong>{document.error && <small>{document.error}</small>}</span></div><span className={`document-status ${document.status}`}>{document.status === "ready" ? `${document.chunk_count} 片段` : document.status === "failed" ? "失败" : document.status === "indexing" ? "索引中" : "等待中"}</span><span>{formatBytes(document.size_bytes)}</span><span>{formatTime(document.updated_at)}</span><div><button type="button" disabled={document.status !== "ready"} onClick={() => previewDocument(document.id)}>预览</button><button type="button" onClick={async () => { await fetchJson(`/knowledge-documents/${document.id}/reindex`, { method: "POST" }); await load(); }}><RefreshCw size={14} /></button><button type="button" onClick={() => setDeleteTarget({ type: "document", item: document })}><Trash2 size={14} /></button></div></div>)}</div>
      </section>
    </div>
    {preview && <Modal title={preview.filename} onClose={() => setPreview(null)}><pre className="document-preview">{preview.content}</pre></Modal>}
    {deleteTarget && <Modal title={deleteTarget.type === "base" ? "删除知识库" : "删除文档"} onClose={() => setDeleteTarget(null)} actions={<><button className="secondary-button" type="button" onClick={() => setDeleteTarget(null)}>取消</button><button className="danger-button" type="button" onClick={() => deleteTarget.type === "base" ? deleteBase(deleteTarget.item) : deleteDocument(deleteTarget.item)}>确认删除</button></>}><p>将删除“{deleteTarget.item.name || deleteTarget.item.filename}”及其本地索引。此操作不能撤销。</p></Modal>}
  </main>;
}

export function App() {
  const [view, setView] = useState("chat");
  const [conversations, setConversations] = useState([]);
  const [knowledgeBases, setKnowledgeBases] = useState([]);
  const [selectedKnowledgeBaseId, setSelectedKnowledgeBaseId] = useState(DEFAULT_KNOWLEDGE_BASE_ID);
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
  const [preview, setPreview] = useState(null);
  const composerRef = useRef(null);
  const bottomRef = useRef(null);

  async function loadModelStatus() {
    try { const state = await window.aegisDesktop?.modelConfig?.status?.(); if (state) setModelStatus(state); }
    catch { setModelStatus({ configured: false }); }
  }
  async function refreshConversations() { const payload = await fetchJson("/conversations"); setConversations(payload.items); return payload.items; }
  async function refreshKnowledgeBases() { const payload = await fetchJson("/knowledge-bases"); setKnowledgeBases(payload.items); return payload.items; }
  async function loadKnowledgeStatus(baseId) { try { setKnowledge(await fetchJson(`/knowledge/status?knowledge_base_id=${encodeURIComponent(baseId)}`)); } catch { setKnowledge(null); } }

  useEffect(() => {
    Promise.all([refreshKnowledgeBases(), refreshConversations(), loadModelStatus(), loadKnowledgeStatus(DEFAULT_KNOWLEDGE_BASE_ID)]).catch((loadError) => setError(loadError.message));
    const shortcut = (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); composerRef.current?.focus(); } };
    window.addEventListener("keydown", shortcut); return () => window.removeEventListener("keydown", shortcut);
  }, []);
  useEffect(() => { loadKnowledgeStatus(selectedKnowledgeBaseId); }, [selectedKnowledgeBaseId]);
  useEffect(() => { const last = messages.at(-1); const target = last?.role === "assistant" ? document.querySelector(".answer-card:last-of-type") : bottomRef.current; target?.scrollIntoView({ behavior: "smooth", block: last?.role === "assistant" ? "start" : "end" }); }, [messages, statusText]);

  const activeConversation = useMemo(() => conversations.find((item) => item.id === activeId), [activeId, conversations]);
  const selectedKnowledgeBase = knowledgeBases.find((item) => item.id === selectedKnowledgeBaseId);

  async function selectConversation(id) {
    if (busy) return; setError(""); const conversation = await fetchJson(`/conversations/${id}`); setActiveId(id); setMessages(conversation.messages); setSelectedKnowledgeBaseId(conversation.knowledge_base_id);
  }
  async function deleteConversation(event, id) { event.stopPropagation(); await fetchJson(`/conversations/${id}`, { method: "DELETE" }); if (activeId === id) { setActiveId(null); setMessages([]); } await refreshConversations(); }
  function newConversation(baseId = selectedKnowledgeBaseId) { if (busy) return; setSelectedKnowledgeBaseId(baseId); setActiveId(null); setMessages([]); setError(""); setView("chat"); window.setTimeout(() => composerRef.current?.focus(), 0); }
  function changeKnowledgeBase(baseId) { if (baseId === selectedKnowledgeBaseId) return; newConversation(baseId); }

  async function previewDocument(documentId) { try { setPreview(await fetchJson(`/knowledge-documents/${documentId}/content`)); } catch (previewError) { setError(previewError.message); } }
  async function submit(rawQuery = query) {
    const value = rawQuery.trim(); if (!value || busy || activeConversation?.knowledge_base_deleted) return;
    setQuery(""); setError(""); setBusy(true); setMessages((current) => [...current, { id: `local-${Date.now()}`, role: "user", query: value }]);
    try {
      await streamChat({ query: value, conversationId: activeId, knowledgeBaseId: selectedKnowledgeBaseId, onEvent: ({ event, data }) => { if (event === "status") setStatusText(data.message); if (event === "answer") { setActiveId(data.conversation_id); setMessages((current) => [...current, { id: data.message_id, role: "assistant", answer: data.answer }]); } if (event === "error") throw new Error(data.message); } });
      await refreshConversations();
    } catch (submitError) { setError(submitError.message || "暂时无法生成回答"); }
    finally { setBusy(false); setStatusText(""); }
  }

  return <div className="app-frame">
    <div className="titlebar"><div className="titlebar-brand"><img src="./app-icon.png" alt="" /><span>AegisCopilot</span><small>v2.1</small></div><span className="titlebar-context">Developer Command RAG</span></div>
    {view === "knowledge" ? <KnowledgeManager knowledgeBases={knowledgeBases} initialId={selectedKnowledgeBaseId} onRefresh={refreshKnowledgeBases} onBack={() => setView("chat")} onSelectForChat={newConversation} /> : <div className="workspace">
      <aside className={`sidebar ${sidebarOpen ? "" : "collapsed"}`}>
        <div className="sidebar-head"><button className="new-chat" type="button" onClick={() => newConversation()}><Plus size={17} /><span>新建对话</span></button><button className="icon-button" type="button" onClick={() => setSidebarOpen(false)} aria-label="收起侧栏"><PanelLeftClose size={18} /></button></div>
        <div className="session-label">最近对话</div>
        <nav className="session-list" aria-label="最近对话">{conversations.length === 0 && <p className="no-sessions">暂无会话</p>}{conversations.map((conversation) => <button key={conversation.id} className={conversation.id === activeId ? "active" : ""} type="button" onClick={() => selectConversation(conversation.id)}><MessageSquareText size={15} /><span>{conversation.title}<small>{conversation.knowledge_base_deleted ? "知识库已删除" : conversation.knowledge_base_name}</small></span><span className="delete-session" role="button" tabIndex={0} onClick={(event) => deleteConversation(event, conversation.id)}><Trash2 size={14} /></span></button>)}</nav>
        <div className="sidebar-status"><button type="button" onClick={() => setView("knowledge")}><FolderCog size={16} /><span>知识库管理</span><small>{knowledgeBases.length} 个知识库</small></button><button type="button" onClick={() => setDrawerOpen(true)}><Settings size={16} /><span>模型设置</span><small>{modelStatus.configured ? "已配置" : "本地模式"}</small></button></div>
      </aside>
      <main className="chat-panel">
        <header className="chat-header"><div className="chat-title-wrap">{!sidebarOpen && <button className="icon-button" type="button" onClick={() => setSidebarOpen(true)} aria-label="展开侧栏"><Menu size={19} /></button>}<div><span className="eyebrow">COMMAND WORKSPACE</span><h2>{activeConversation?.title || "新的技术问题"}</h2></div></div><div className="knowledge-select-wrap"><BookOpen size={16} /><select value={selectedKnowledgeBaseId} onChange={(event) => changeKnowledgeBase(event.target.value)} disabled={busy}>{knowledgeBases.map((base) => <option key={base.id} value={base.id}>{base.name}</option>)}</select><span className={`status-dot ${knowledge?.ready ? "ready" : ""}`} /></div></header>
        <div className="message-viewport">{messages.length === 0 ? <EmptyState onExample={submit} /> : <div className="message-column">{activeConversation?.knowledge_base_deleted && <div className="error-banner"><AlertTriangle size={17} /><span>此会话的知识库已删除，只能阅读历史记录。</span></div>}{messages.map((message) => message.role === "user" ? <UserMessage key={message.id} message={message} /> : <AnswerCard key={message.id} answer={message.answer} onPreviewDocument={previewDocument} />)}{busy && <div className="thinking"><LoaderCircle size={17} /><span>{statusText || "正在准备回答"}</span></div>}{error && <div className="error-banner"><WifiOff size={17} /><span>{error}</span></div>}<div ref={bottomRef} /></div>}</div>
        <div className="composer-wrap">{error && messages.length === 0 && <div className="error-banner"><WifiOff size={17} /><span>{error}</span></div>}<div className="composer"><textarea ref={composerRef} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submit(); } }} placeholder={`向“${selectedKnowledgeBase?.name || "知识库"}”提问`} rows={1} disabled={busy || activeConversation?.knowledge_base_deleted} /><button type="button" onClick={() => submit()} disabled={busy || !query.trim() || activeConversation?.knowledge_base_deleted} aria-label="发送问题"><Send size={18} /></button></div><div className="composer-foot"><span>Ctrl + Enter 发送</span><span>命令仅供复制，不会自动执行</span></div></div>
      </main>
    </div>}
    <SettingsDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} modelStatus={modelStatus} onChanged={loadModelStatus} />
    {preview && <Modal title={preview.filename} onClose={() => setPreview(null)}><pre className="document-preview">{preview.content}</pre></Modal>}
  </div>;
}
