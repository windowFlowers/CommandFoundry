import { useId, useRef, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  Box,
  BrainCircuit,
  Check,
  ChevronDown,
  Clipboard,
  Code2,
  Database,
  FileCode2,
  FolderCog,
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
} from "lucide-react";
import { contextBadgeLabel } from "../../lib/memory";

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
      <span aria-live="polite">{copied ? "已复制" : "复制"}</span>
    </button>
  );
}

function CommandCard({ command, index }) {
  const titleId = useId();
  return (
    <section className={`command-card risk-${command.risk}`} aria-labelledby={titleId}>
      <header className="command-head">
        <div><span className="command-index">{String(index + 1).padStart(2, "0")}</span><h3 id={titleId}>{command.label}</h3></div>
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

function ContextBadge({ answer, onOpen }) {
  const label = contextBadgeLabel(answer);
  if (!label) return null;
  return <button className="context-badge" type="button" onClick={() => onOpen(answer.context)} aria-label={`查看${label}`} data-ui="context-badge"><BrainCircuit size={13} />{label}</button>;
}

function AnswerCard({ answer, entryNumber, onPreviewDocument, onOpenMemory }) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const titleId = useId();
  return (
    <article className="answer-card" aria-labelledby={titleId} data-ui="answer-card">
      <div className="answer-topline">
        <span className="thread-index">{entryNumber}</span>
        <span className="assistant-mark"><Code2 size={17} /></span>
        <h2 className="speaker-name" id={titleId}>AegisCopilot</h2>
        <ContextBadge answer={answer} onOpen={onOpenMemory} />
        <GenerationBadge answer={answer} />
      </div>
      <p className="answer-summary">{answer.summary}</p>
      {answer.commands?.length > 0 && (
        <div className="commands-list">{answer.commands.map((command, index) => <CommandCard key={`${command.label}-${index}`} command={command} index={index} />)}</div>
      )}
      {answer.notes?.length > 0 && <ul className="notes-list">{answer.notes.map((note, index) => <li key={`${note}-${index}`}>{note}</li>)}</ul>}
      {answer.citations?.length > 0 && (
        <div className="citations">
          <button className="citations-toggle" type="button" onClick={() => setSourcesOpen((value) => !value)} aria-expanded={sourcesOpen}>
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

function UserMessage({ message, entryNumber }) {
  const titleId = useId();
  return (
    <article className="user-message" aria-labelledby={titleId}>
      <div className="user-message-head"><span className="thread-index">{entryNumber}</span><span className="user-mark">你</span><h2 className="speaker-name" id={titleId}>问题</h2><CopyButton value={message.query} kind="问题" compact /></div>
      <p>{message.query}</p>
    </article>
  );
}

function EmptyState({ onExample }) {
  return (
    <section className="empty-state" data-ui="empty-state">
      <div className="empty-copy">
        <span className="eyebrow">LOCAL-FIRST COMMAND RAG</span>
        <h2>把问题变成<br />可以直接使用的命令</h2>
        <p>从所选知识库检索可靠用法，再由模型整理上下文。命令会标注平台、前置条件、风险和来源。</p>
        <div className="exhibition-meta"><span>CURATED LOCALLY</span><span>TRACEABLE SOURCES</span><span>SAFE BY DEFAULT</span></div>
      </div>
      <section className="example-catalogue" aria-labelledby="example-catalogue-title">
        <div className="catalogue-heading"><h2 id="example-catalogue-title">示例目录</h2><span>06 ENTRIES</span></div>
        <div className="example-grid" data-ui="example-grid">
          {examples.map(({ id, label, question, icon: Icon }, index) => (
            <button key={id} type="button" onClick={() => onExample(question)}>
              <span className="example-number">{String(index + 1).padStart(2, "0")}</span>
              <span className="example-icon"><Icon size={18} /></span>
              <span className="example-content"><strong>{label}</strong><small>{question}</small></span>
            </button>
          ))}
        </div>
      </section>
    </section>
  );
}

function Sidebar({ open, conversations, activeId, onNewConversation, onSelectConversation, onDeleteConversation, onCollapse, collapseRef, onOpenKnowledge, knowledgeBaseCount, onOpenSettings, modelStatus }) {
  return (
    <aside className={`sidebar ${open ? "" : "collapsed"}`} aria-hidden={!open} inert={open ? undefined : ""} data-ui="sidebar">
      <div className="sidebar-head">
        <button className="new-chat" type="button" onClick={onNewConversation} data-ui="new-chat"><Plus size={17} /><span>新建对话</span></button>
        <button ref={collapseRef} className="icon-button" type="button" onClick={onCollapse} aria-label="收起侧栏"><PanelLeftClose size={18} /></button>
      </div>
      <div className="session-label"><span>最近对话</span><span>{String(conversations.length).padStart(2, "0")}</span></div>
      <nav className="session-list" aria-label="最近对话">
        {conversations.length === 0 && <p className="no-sessions">暂无会话</p>}
        {conversations.map((conversation, index) => (
          <div className={`session-item ${conversation.id === activeId ? "active" : ""}`} key={conversation.id} data-conversation-id={conversation.id}>
            <button className="session-select" type="button" onClick={() => onSelectConversation(conversation.id)} aria-current={conversation.id === activeId ? "page" : undefined}>
              <span className="session-number">{String(index + 1).padStart(2, "0")}</span>
              <MessageSquareText size={15} />
              <span>{conversation.title}<small>{conversation.knowledge_base_deleted ? "知识库已删除" : conversation.knowledge_base_name}</small></span>
            </button>
            <button className="delete-session" type="button" onClick={() => onDeleteConversation(conversation.id)} aria-label={`删除对话：${conversation.title}`}><Trash2 size={14} /></button>
          </div>
        ))}
      </nav>
      <div className="sidebar-status">
        <button type="button" onClick={onOpenKnowledge} data-ui="open-knowledge"><FolderCog size={17} /><span>知识库管理</span><small>{knowledgeBaseCount} 个知识库</small></button>
        <button type="button" onClick={onOpenSettings} data-ui="open-settings"><Settings size={17} /><span>模型设置</span><small>{modelStatus.configured ? "已配置" : "本地模式"}</small></button>
      </div>
    </aside>
  );
}

function ChatPanel({ sidebarOpen, onExpandSidebar, expandRef, activeConversation, selectedKnowledgeBaseId, onKnowledgeBaseChange, knowledgeBases, knowledgeStatus, messages, answerAnnouncement, busy, statusText, error, onPreviewDocument, onOpenMemory, onSubmit, query, onQueryChange, composerRef, bottomRef }) {
  const selectedKnowledgeBase = knowledgeBases.find((item) => item.id === selectedKnowledgeBaseId);
  const deletedKnowledgeBase = activeConversation?.knowledge_base_deleted
    && activeConversation.knowledge_base_id === selectedKnowledgeBaseId;
  const composerDisabled = busy || activeConversation?.knowledge_base_deleted;

  return (
    <main className="chat-panel" data-ui="chat-panel">
      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">{answerAnnouncement}</p>
      <header className="chat-header">
        <div className="chat-title-wrap">
          {!sidebarOpen && <button ref={expandRef} className="icon-button" type="button" onClick={onExpandSidebar} aria-label="展开侧栏" data-ui="expand-sidebar"><Menu size={19} /></button>}
          <div><span className="eyebrow">COMMAND WORKSPACE</span><h1>{activeConversation?.title || "新的技术问题"}</h1></div>
        </div>
        <label className="knowledge-select-wrap">
          <BookOpen size={16} />
          <span className="sr-only">选择知识库</span>
          <select value={selectedKnowledgeBaseId} onChange={(event) => onKnowledgeBaseChange(event.target.value)} disabled={busy} data-ui="knowledge-select">
            {deletedKnowledgeBase && <option value={selectedKnowledgeBaseId}>{activeConversation.knowledge_base_name}（已删除）</option>}
            {knowledgeBases.map((base) => <option key={base.id} value={base.id}>{base.name}</option>)}
          </select>
          <span className={`status-dot ${knowledgeStatus?.ready && !deletedKnowledgeBase ? "ready" : ""}`} aria-hidden="true" />
          <span className="knowledge-status-label" role="status">{deletedKnowledgeBase ? "已删除" : knowledgeStatus?.ready ? "已就绪" : "准备中"}</span>
        </label>
      </header>
      <div className="message-viewport">
        {messages.length === 0 ? <EmptyState onExample={onSubmit} /> : (
          <div className="message-column">
            {activeConversation?.knowledge_base_deleted && <div className="error-banner" role="alert"><AlertTriangle size={17} /><span>此会话的知识库已删除，只能阅读历史记录。</span></div>}
            {messages.map((message, index) => message.role === "user" ? (
              <UserMessage key={message.id} message={message} entryNumber={String(index + 1).padStart(2, "0")} />
            ) : (
              <AnswerCard key={message.id} answer={message.answer} entryNumber={String(index + 1).padStart(2, "0")} onPreviewDocument={onPreviewDocument} onOpenMemory={onOpenMemory} />
            ))}
            {busy && <div className="thinking" role="status"><LoaderCircle size={17} /><span>{statusText || "正在准备回答"}</span></div>}
            {error && <div className="error-banner" role="alert"><WifiOff size={17} /><span>{error}</span></div>}
            <div ref={bottomRef} />
          </div>
        )}
      </div>
      <div className="composer-wrap">
        {error && messages.length === 0 && <div className="error-banner" role="alert"><WifiOff size={17} /><span>{error}</span></div>}
        <div className="composer-shell">
          <label className="composer-label" htmlFor="chat-query"><span>QUERY</span><span>向“{selectedKnowledgeBase?.name || "知识库"}”提问</span></label>
          <div className="composer" data-ui="composer">
            <textarea
              id="chat-query"
              ref={composerRef}
              value={query}
              onChange={(event) => onQueryChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                  event.preventDefault();
                  onSubmit();
                }
              }}
              placeholder="描述你要完成的操作"
              rows={1}
              disabled={composerDisabled}
            />
            <button type="button" onClick={() => onSubmit()} disabled={composerDisabled || !query.trim()} aria-label="发送问题"><Send size={18} /></button>
          </div>
          <div className="composer-foot"><span>Ctrl + Enter 发送</span><span>命令仅供复制，不会自动执行</span></div>
        </div>
      </div>
    </main>
  );
}

export function ChatWorkspace({ sidebarOpen, onSidebarOpenChange, conversations, activeId, onNewConversation, onSelectConversation, onDeleteConversation, onOpenKnowledge, knowledgeBases, onOpenSettings, modelStatus, activeConversation, selectedKnowledgeBaseId, onKnowledgeBaseChange, knowledgeStatus, messages, answerAnnouncement, busy, statusText, error, onPreviewDocument, onOpenMemory, onSubmit, query, onQueryChange, composerRef, bottomRef }) {
  const expandRef = useRef(null);
  const collapseRef = useRef(null);

  function collapseSidebar() {
    onSidebarOpenChange(false);
    window.requestAnimationFrame(() => expandRef.current?.focus());
  }

  function expandSidebar() {
    onSidebarOpenChange(true);
    window.requestAnimationFrame(() => collapseRef.current?.focus());
  }

  return (
    <div className="workspace">
      <Sidebar
        open={sidebarOpen}
        conversations={conversations}
        activeId={activeId}
        onNewConversation={onNewConversation}
        onSelectConversation={onSelectConversation}
        onDeleteConversation={onDeleteConversation}
        onCollapse={collapseSidebar}
        collapseRef={collapseRef}
        onOpenKnowledge={onOpenKnowledge}
        knowledgeBaseCount={knowledgeBases.length}
        onOpenSettings={onOpenSettings}
        modelStatus={modelStatus}
      />
      <ChatPanel
        sidebarOpen={sidebarOpen}
        onExpandSidebar={expandSidebar}
        expandRef={expandRef}
        activeConversation={activeConversation}
        selectedKnowledgeBaseId={selectedKnowledgeBaseId}
        onKnowledgeBaseChange={onKnowledgeBaseChange}
        knowledgeBases={knowledgeBases}
        knowledgeStatus={knowledgeStatus}
        messages={messages}
        answerAnnouncement={answerAnnouncement}
        busy={busy}
        statusText={statusText}
        error={error}
        onPreviewDocument={onPreviewDocument}
        onOpenMemory={onOpenMemory}
        onSubmit={onSubmit}
        query={query}
        onQueryChange={onQueryChange}
        composerRef={composerRef}
        bottomRef={bottomRef}
      />
    </div>
  );
}
