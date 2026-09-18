import { useId, useRef, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  BrainCircuit,
  Check,
  ChevronDown,
  Clipboard,
  Code2,
  FileCode2,
  FolderCog,
  GitBranch,
  LoaderCircle,
  Menu,
  MessageSquareText,
  PanelLeftClose,
  Plus,
  Send,
  Settings,
  Shell,
  Trash2,
  UserRound,
  WifiOff,
} from "lucide-react";
import { contextBadgeLabel } from "../../lib/memory";
import { personalizationBadgeLabel } from "../../lib/profile";
import { SelectMenu } from "../ui/SelectMenu";

const examples = [
  { id: "linux", label: "Linux / Shell", question: "在 Linux 中切换文件目录的指令是什么？", icon: Shell },
  { id: "git", label: "Git", question: "Git 怎么安全回滚一次提交？", icon: GitBranch },
  { id: "python", label: "Python", question: "Python 怎么创建并激活虚拟环境？", icon: FileCode2 },
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
const shellText = {
  powershell: "POWERSHELL",
  cmd: "CMD",
  bash: "BASH",
  posix: "POSIX SHELL",
  sql: "SQL",
  text: "TEXT",
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

function CitationMarker({ reference, onPreviewDocument, compact = false }) {
  if (!reference) return null;
  const label = `[${reference.number}]`;
  const className = `inline-citation ${compact ? "compact" : ""}`;
  if (reference.citation.source_url) {
    return <a className={className} href={reference.citation.source_url} target="_blank" rel="noreferrer" aria-label={`打开引用 ${reference.number}`}>{label}</a>;
  }
  if (reference.citation.document_id) {
    return <button className={className} type="button" onClick={() => onPreviewDocument(reference.citation)} aria-label={`定位引用 ${reference.number}`}>{label}</button>;
  }
  return <span className={className}>{label}</span>;
}

function referencesForIds(citationIds, citationMap) {
  return [...new Set(citationIds || [])].map((id) => citationMap.get(id)).filter(Boolean);
}

function CommandCard({ command, index, citationMap, onPreviewDocument }) {
  const titleId = useId();
  const references = referencesForIds(command.citation_ids, citationMap);
  return (
    <section className={`command-card risk-${command.risk}`} aria-labelledby={titleId}>
      <header className="command-head">
        <div><span className="command-index">{String(index + 1).padStart(2, "0")}</span><h3 id={titleId}>{command.label}</h3></div>
        <span className={`risk-badge ${command.risk}`}>{riskText[command.risk] || command.risk}</span>
      </header>
      <div className="code-shell">
        <div className="code-toolbar"><span>{shellText[command.shell] || command.language}</span><CopyButton value={command.code} /></div>
        <pre><code>{command.code}</code></pre>
      </div>
      <div className="command-meta">
        {command.platforms?.length > 0 && <div><span className="meta-label">运行平台</span><span>{command.platforms.join(" · ")}</span></div>}
        {command.prerequisites?.length > 0 && <div><span className="meta-label">执行前</span><span>{command.prerequisites.join("；")}</span></div>}
        {command.warning && <div className="warning-row"><AlertTriangle size={15} /><span>{command.warning}</span></div>}
        {references.length > 0 && <div className="command-citations"><span className="meta-label">引用</span><span>{references.map((reference) => <CitationMarker key={reference.citation.citation_id} reference={reference} onPreviewDocument={onPreviewDocument} compact />)}</span></div>}
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

function PersonalizationBadge({ answer, onOpen }) {
  const label = personalizationBadgeLabel(answer);
  if (!label) return null;
  return <button className="personalization-badge" type="button" onClick={() => onOpen(answer.personalization)} aria-label={`查看本次个性化，使用 ${answer.personalization.memory_ids.length} 条记忆`} data-ui="personalization-badge"><UserRound size={13} />{label}</button>;
}

function normalizeClarificationOption(option, index) {
  if (typeof option === "string") return { label: option, value: option, id: `${option}-${index}` };
  const value = option?.value ?? option?.label ?? "";
  const label = option?.label ?? value;
  return { label: String(label), value: String(value), id: option?.id || `${value}-${index}` };
}

function clarificationInputType(clarification) {
  const inputType = clarification?.input_type || clarification?.type || "text";
  return ["path", "number", "select"].includes(inputType) ? inputType : "text";
}

function ClarificationCard({ clarification, onSubmit, disabled = false, stale = false }) {
  const inputId = useId();
  const errorId = useId();
  const [value, setValue] = useState("");
  const [validationError, setValidationError] = useState("");
  const inputType = clarificationInputType(clarification);
  const options = (clarification?.options || []).map(normalizeClarificationOption).filter((option) => option.value);
  const locked = disabled || stale;
  const hasInput = inputType !== "select";
  const step = clarification?.step ?? clarification?.slot_index;
  const totalSlots = clarification?.total_slots ?? clarification?.slot_count;
  const placeholder = clarification?.placeholder || (inputType === "path" ? "输入完整路径" : inputType === "number" ? "输入数字" : "输入你的选择");
  const allowTemplate = clarification?.allow_template !== false;

  function submitValue(candidate) {
    if (locked) return;
    const normalized = String(candidate ?? "").trim();
    if (!normalized) {
      setValidationError("请先填写信息，或选择一个快捷选项。");
      return;
    }
    setValidationError("");
    onSubmit?.(normalized);
  }

  function submitInput(event) {
    event.preventDefault();
    submitValue(value);
  }

  return (
    <section className={`clarification-card ${locked ? "locked" : ""}`} role="group" aria-labelledby={inputId} aria-describedby={validationError ? errorId : undefined} aria-busy={disabled} data-ui="clarification-card">
      <header className="clarification-head">
        <span className="clarification-kicker">需要确认</span>
        {(step || totalSlots) && <span className="clarification-progress">{step && totalSlots ? `${step} / ${totalSlots}` : step ? `第 ${step} 项` : `共 ${totalSlots} 项`}</span>}
      </header>
      <h3 id={inputId}>{clarification?.question || "请补充信息"}</h3>
      {clarification?.description && <p className="clarification-help">{clarification.description}</p>}
      {options.length > 0 && (
        <div className="clarification-options" role="group" aria-label="快捷选项">
          {options.map((option) => (
            <button key={option.id} className="clarification-option" type="button" disabled={locked} onClick={() => submitValue(option.value)}>{option.label}</button>
          ))}
        </div>
      )}
      {hasInput && (
        <form className="clarification-form" onSubmit={submitInput}>
          <label htmlFor={`${inputId}-value`}>{options.length > 0 ? "或填写自定义值" : "你的回答"}</label>
          <div className="clarification-input-row">
            <input
              id={`${inputId}-value`}
              type={inputType === "number" ? "number" : "text"}
              inputMode={inputType === "number" ? "decimal" : undefined}
              step={inputType === "number" ? "1" : undefined}
              value={value}
              onChange={(event) => { setValue(event.target.value); if (validationError) setValidationError(""); }}
              placeholder={placeholder}
              disabled={locked}
              aria-invalid={Boolean(validationError)}
              aria-describedby={validationError ? errorId : undefined}
              autoComplete="off"
            />
            <button className="primary-button clarification-submit" type="submit" disabled={locked || !value.trim()}>确认</button>
          </div>
        </form>
      )}
      {validationError && <p id={errorId} className="clarification-error" role="alert">{validationError}</p>}
      {allowTemplate && <div className="clarification-footer"><button className="secondary-button clarification-template" type="button" disabled={locked} onClick={() => submitValue("给我一个模板示例")}>给我一个模板示例</button></div>}
      {stale && <p className="clarification-locked" role="status">已进入下一步，不能重复提交此问题。</p>}
    </section>
  );
}

function AnswerCard({ answer, entryNumber, onPreviewDocument, onOpenMemory, onOpenPersonalization, onSubmit, busy = false, interactive = false }) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const titleId = useId();
  const citations = answer.citations || [];
  const citationMap = new Map(
    citations
      .map((citation, index) => [citation.citation_id, { citation, number: index + 1 }])
      .filter(([citationId]) => Boolean(citationId)),
  );
  const segments = (answer.summary_segments || []).filter((segment) => segment?.text);
  const answerKind = answer.answer_kind === "clarification" ? "clarification" : answer.answer_kind === "direct" ? "direct" : "template";
  const visibleCommands = (answer.commands || []).slice(0, 1);
  const clarification = answer.clarification || {};
  return (
    <article className={`answer-card answer-${answerKind}`} aria-labelledby={titleId} data-ui="answer-card">
      <div className="answer-topline">
        <span className="thread-index">{entryNumber}</span>
        <span className="assistant-mark"><Code2 size={17} /></span>
        <h2 className="speaker-name" id={titleId}>AegisCopilot</h2>
        <div className="answer-signals">
          <ContextBadge answer={answer} onOpen={onOpenMemory} />
          <PersonalizationBadge answer={answer} onOpen={onOpenPersonalization} />
          <GenerationBadge answer={answer} />
        </div>
      </div>
      {segments.length > 0 ? (
        <p className="answer-summary answer-segments">{segments.map((segment, segmentIndex) => (
          <span key={`${segment.text}-${segmentIndex}`}>
            {segment.text}
            {referencesForIds(segment.citation_ids, citationMap).map((reference) => <CitationMarker key={`${segmentIndex}-${reference.citation.citation_id}`} reference={reference} onPreviewDocument={onPreviewDocument} />)}
          </span>
        ))}</p>
      ) : <p className="answer-summary">{answer.summary}</p>}
      {answerKind === "clarification" && <ClarificationCard clarification={clarification} onSubmit={onSubmit} disabled={busy} stale={!interactive} />}
      {answerKind === "template" && <div className="answer-kind-note" data-ui="template-note"><span>模板示例</span><span>需要替换参数</span></div>}
      {answerKind !== "clarification" && visibleCommands.length > 0 && (
        <div className="commands-list">{visibleCommands.map((command, index) => <CommandCard key={`${command.label}-${index}`} command={command} index={index} citationMap={citationMap} onPreviewDocument={onPreviewDocument} />)}</div>
      )}
      {answer.notes?.length > 0 && <ul className="notes-list">{answer.notes.map((note, index) => <li key={`${note}-${index}`}>{note}</li>)}</ul>}
      {citations.length > 0 && (
        <div className="citations">
          <button className="citations-toggle" type="button" onClick={() => setSourcesOpen((value) => !value)} aria-expanded={sourcesOpen}>
            <BookOpen size={16} /><span>{citations.length} 个可追溯来源</span><ChevronDown className={sourcesOpen ? "rotate" : ""} size={16} />
          </button>
          {sourcesOpen && (
            <div className="citation-list">
              {citations.map((citation, index) => {
                const content = <><div className="citation-card-topline"><span className="citation-card-index">[{index + 1}]</span><span className="citation-domain">{domainText[citation.domain] || citation.domain}</span></div><strong>{citation.title}</strong><small>{citation.license} · {citation.revision?.slice(0, 12)}</small><p>{citation.excerpt}</p></>;
                return citation.source_url ? (
                  <a key={`${citation.source_id}-${index}`} href={citation.source_url} target="_blank" rel="noreferrer">{content}</a>
                ) : (
                  <button key={`${citation.source_id}-${index}`} type="button" disabled={!citation.document_id} onClick={() => citation.document_id && onPreviewDocument(citation)}>{content}</button>
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
      <section className="example-catalogue" aria-labelledby="example-catalogue-title">
        <div className="catalogue-heading"><h2 id="example-catalogue-title">示例目录</h2><span>03 ENTRIES</span></div>
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

function Sidebar({ open, conversations, activeId, onNewConversation, onSelectConversation, onDeleteConversation, onCollapse, collapseRef, onOpenKnowledge, knowledgeBaseCount, onOpenProfile, profile, onOpenSettings, modelStatus }) {
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
        <button type="button" onClick={onOpenProfile} data-ui="open-profile"><UserRound size={17} /><span>我的记忆</span><small>{profile ? profile.personalization_enabled ? `${profile.active_memory_count || 0} 条` : "已关闭" : "读取中"}</small></button>
        <button type="button" onClick={onOpenSettings} data-ui="open-settings"><Settings size={17} /><span>模型设置</span><small>{modelStatus.configured ? "已配置" : "本地模式"}</small></button>
      </div>
    </aside>
  );
}

function ChatPanel({ sidebarOpen, onExpandSidebar, expandRef, activeConversation, selectedKnowledgeBaseId, onKnowledgeBaseChange, knowledgeBases, knowledgeStatus, messages, answerAnnouncement, busy, statusText, error, onPreviewDocument, onOpenMemory, onOpenPersonalization, onSubmit, query, onQueryChange, composerRef, bottomRef }) {
  const selectedKnowledgeBase = knowledgeBases.find((item) => item.id === selectedKnowledgeBaseId);
  const deletedKnowledgeBase = activeConversation?.knowledge_base_deleted
    && activeConversation.knowledge_base_id === selectedKnowledgeBaseId;
  const composerDisabled = busy || activeConversation?.knowledge_base_deleted;
  const lastMessage = messages.at(-1);
  const latestClarificationId = lastMessage?.role === "assistant" && lastMessage.answer?.answer_kind === "clarification"
    ? lastMessage.id
    : null;

  return (
    <main className="chat-panel" data-ui="chat-panel">
      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">{answerAnnouncement}</p>
      <header className="chat-header">
        <div className="chat-title-wrap">
          {!sidebarOpen && <button ref={expandRef} className="icon-button" type="button" onClick={onExpandSidebar} aria-label="展开侧栏" data-ui="expand-sidebar"><Menu size={19} /></button>}
          <div><span className="eyebrow">COMMAND WORKSPACE</span><h1>{activeConversation?.title || "新的技术问题"}</h1></div>
        </div>
        <div className="knowledge-select-wrap">
          <BookOpen size={16} aria-hidden="true" />
          <SelectMenu
            value={selectedKnowledgeBaseId}
            options={[
              ...(deletedKnowledgeBase ? [{ value: selectedKnowledgeBaseId, label: `${activeConversation.knowledge_base_name}（已删除）` }] : []),
              ...knowledgeBases.map((base) => ({ value: base.id, label: base.name })),
            ]}
            onChange={onKnowledgeBaseChange}
            ariaLabel="选择知识库"
            disabled={busy}
            dataUi="knowledge-select"
          />
          <span className={`status-dot ${knowledgeStatus?.ready && !deletedKnowledgeBase ? "ready" : ""}`} aria-hidden="true" />
          <span className="knowledge-status-label" role="status">{deletedKnowledgeBase ? "已删除" : knowledgeStatus?.ready ? "已就绪" : "准备中"}</span>
        </div>
      </header>
      <div className="message-viewport">
        {messages.length === 0 ? <EmptyState onExample={onSubmit} /> : (
          <div className="message-column">
            {activeConversation?.knowledge_base_deleted && <div className="error-banner" role="alert"><AlertTriangle size={17} /><span>此会话的知识库已删除，只能阅读历史记录。</span></div>}
            {messages.map((message, index) => message.role === "user" ? (
              <UserMessage key={message.id} message={message} entryNumber={String(index + 1).padStart(2, "0")} />
            ) : (
              <AnswerCard key={message.id} answer={message.answer} entryNumber={String(index + 1).padStart(2, "0")} onPreviewDocument={onPreviewDocument} onOpenMemory={onOpenMemory} onOpenPersonalization={onOpenPersonalization} onSubmit={onSubmit} busy={busy} interactive={message.id === latestClarificationId} />
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

export function ChatWorkspace({ sidebarOpen, onSidebarOpenChange, conversations, activeId, onNewConversation, onSelectConversation, onDeleteConversation, onOpenKnowledge, knowledgeBases, onOpenProfile, profile, onOpenSettings, modelStatus, activeConversation, selectedKnowledgeBaseId, onKnowledgeBaseChange, knowledgeStatus, messages, answerAnnouncement, busy, statusText, error, onPreviewDocument, onOpenMemory, onOpenPersonalization, onSubmit, query, onQueryChange, composerRef, bottomRef }) {
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
        onOpenProfile={onOpenProfile}
        profile={profile}
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
        onOpenPersonalization={onOpenPersonalization}
        onSubmit={onSubmit}
        query={query}
        onQueryChange={onQueryChange}
        composerRef={composerRef}
        bottomRef={bottomRef}
      />
    </div>
  );
}
