import { useEffect, useMemo, useRef, useState } from "react";
import { ChatWorkspace } from "./components/chat/ChatWorkspace";
import { DocumentPreviewModal, KnowledgeManager } from "./components/knowledge/KnowledgeManager";
import { MemoryDrawer, SettingsDrawer } from "./components/overlays/AppDrawers";
import { PersonalizationDrawer } from "./components/overlays/PersonalizationDrawer";
import { ProfileMemoryManager } from "./components/profile/ProfileMemoryManager";
import { TerminalPanel } from "./components/terminal/TerminalPanel";
import { Modal } from "./components/ui/Overlays";
import { ToastRegion } from "./components/ui/ToastRegion";
import { useProfileMemory } from "./hooks/useProfileMemory";
import { fetchJson, streamChat } from "./lib/api";
import { appendStreamAnswer, memoryStateForAnswer, resetConversationMemory } from "./lib/memory";

const DEFAULT_KNOWLEDGE_BASE_ID = "developer-it";

export function App() {
  const appVersion = window.aegisDesktop?.version || "2.9.0";
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
  const [answerAnnouncement, setAnswerAnnouncement] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [memoryDrawerOpen, setMemoryDrawerOpen] = useState(false);
  const [memoryState, setMemoryState] = useState(null);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const [memoryResetOpen, setMemoryResetOpen] = useState(false);
  const [personalizationDrawerOpen, setPersonalizationDrawerOpen] = useState(false);
  const [personalizationState, setPersonalizationState] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 900);
  const [preview, setPreview] = useState(null);
  const [terminalOpen, setTerminalOpen] = useState(false);
  const {
    profile,
    memoryRevision,
    toast,
    memoriesChanged,
    notifyMemoryUpdate,
    dismissToast,
    undoToast,
  } = useProfileMemory();
  const composerRef = useRef(null);
  const bottomRef = useRef(null);
  const memoryResetCancelRef = useRef(null);

  async function loadModelStatus() {
    try {
      const state = await window.aegisDesktop?.modelConfig?.status?.();
      if (state) setModelStatus(state);
    } catch {
      setModelStatus({ configured: false });
    }
  }

  async function refreshConversations() {
    const payload = await fetchJson("/conversations");
    setConversations(payload.items);
    return payload.items;
  }

  async function refreshKnowledgeBases() {
    const payload = await fetchJson("/knowledge-bases");
    setKnowledgeBases(payload.items);
    return payload.items;
  }

  async function loadKnowledgeStatus(baseId) {
    try {
      setKnowledge(await fetchJson(`/knowledge/status?knowledge_base_id=${encodeURIComponent(baseId)}`));
    } catch {
      setKnowledge(null);
    }
  }

  useEffect(() => {
    Promise.all([
      refreshKnowledgeBases(),
      refreshConversations(),
      loadModelStatus(),
      loadKnowledgeStatus(DEFAULT_KNOWLEDGE_BASE_ID),
    ]).catch((loadError) => setError(loadError.message));

    const shortcut = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (document.querySelector('[aria-modal="true"]:not([aria-hidden="true"])')) return;
        setView("chat");
        window.requestAnimationFrame(() => composerRef.current?.focus());
      }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);

  useEffect(() => { loadKnowledgeStatus(selectedKnowledgeBaseId); }, [selectedKnowledgeBaseId]);
  useEffect(() => {
    const last = messages.at(-1);
    const target = last?.role === "assistant" ? document.querySelector('[data-ui="answer-card"]:last-of-type') : bottomRef.current;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: last?.role === "assistant" ? "start" : "end" });
  }, [messages, statusText]);

  const activeConversation = useMemo(() => conversations.find((item) => item.id === activeId), [activeId, conversations]);

  async function selectConversation(id) {
    if (busy) return;
    setError("");
    setMemoryDrawerOpen(false);
    setPersonalizationDrawerOpen(false);
    const conversation = await fetchJson(`/conversations/${id}`);
    setActiveId(id);
    setMessages(conversation.messages);
    setAnswerAnnouncement("");
    setSelectedKnowledgeBaseId(conversation.knowledge_base_id);
  }

  async function deleteConversation(id) {
    await fetchJson(`/conversations/${id}`, { method: "DELETE" });
    if (activeId === id) {
      setActiveId(null);
      setMessages([]);
      setAnswerAnnouncement("");
    }
    await refreshConversations();
  }

  function newConversation(baseId = selectedKnowledgeBaseId) {
    if (busy) return;
    const nextBaseId = knowledgeBases.some((base) => base.id === baseId) ? baseId : DEFAULT_KNOWLEDGE_BASE_ID;
    setSelectedKnowledgeBaseId(nextBaseId);
    setActiveId(null);
    setMessages([]);
    setAnswerAnnouncement("");
    setError("");
    setMemoryDrawerOpen(false);
    setPersonalizationDrawerOpen(false);
    setView("chat");
    window.setTimeout(() => composerRef.current?.focus(), 0);
  }

  function changeKnowledgeBase(baseId) {
    if (baseId === selectedKnowledgeBaseId) return;
    newConversation(baseId);
  }

  async function handleKnowledgeBaseDeleted(deletedId) {
    const updatedConversations = await refreshConversations();
    const updatedActive = updatedConversations.find((conversation) => conversation.id === activeId);
    if (!updatedActive || updatedActive.knowledge_base_id !== deletedId) {
      setSelectedKnowledgeBaseId(DEFAULT_KNOWLEDGE_BASE_ID);
    }
    await memoriesChanged();
  }

  async function previewDocument(source) {
    const citation = typeof source === "string" ? null : source;
    const documentId = citation?.document_id || source;
    if (!documentId) return;
    try {
      const chunkQuery = citation?.chunk_id ? `?chunk_id=${encodeURIComponent(citation.chunk_id)}` : "";
      const payload = await fetchJson(`/knowledge-documents/${documentId}/content${chunkQuery}`);
      const versionChanged = Boolean(
        payload.version_changed
        || (citation?.index_revision && payload.index_revision && citation.index_revision !== payload.index_revision)
        || (citation?.document_sha256 && payload.document_sha256 && citation.document_sha256 !== payload.document_sha256),
      );
      setPreview({ ...payload, version_changed: versionChanged });
    } catch (previewError) {
      if (citation) {
        setPreview({
          filename: citation.title,
          content: citation.excerpt || "",
          locator: citation.locator || null,
          highlight_start: 0,
          highlight_end: Array.from(citation.excerpt || "").length,
          source_unavailable: true,
        });
      } else {
        setError(previewError.message);
      }
    }
  }

  async function openMemory(answerContext) {
    if (!activeId) return;
    setSettingsOpen(false);
    setPersonalizationDrawerOpen(false);
    setMemoryDrawerOpen(true);
    setMemoryLoading(true);
    setMemoryError("");
    try {
      const memory = await fetchJson(`/conversations/${activeId}/memory`);
      setMemoryState(memoryStateForAnswer(memory, messages, answerContext));
    } catch (loadError) {
      setMemoryError(loadError.message);
    } finally {
      setMemoryLoading(false);
    }
  }

  function openPersonalization(personalization) {
    setSettingsOpen(false);
    setMemoryDrawerOpen(false);
    setPersonalizationState(personalization);
    setPersonalizationDrawerOpen(true);
  }

  async function resetMemory() {
    if (!activeId) return;
    try {
      await resetConversationMemory(activeId);
      setMemoryResetOpen(false);
      setMemoryState(await fetchJson(`/conversations/${activeId}/memory`));
    } catch (resetError) {
      setMemoryError(resetError.message);
      setMemoryResetOpen(false);
    }
  }

  async function submit(rawQuery = query) {
    const value = rawQuery.trim();
    if (!value || busy || activeConversation?.knowledge_base_deleted) return;
    setQuery("");
    setError("");
    setAnswerAnnouncement("");
    setBusy(true);
    setMessages((current) => [...current, { id: `local-${Date.now()}`, role: "user", query: value }]);
    try {
      await streamChat({
        query: value,
        conversationId: activeId,
        knowledgeBaseId: selectedKnowledgeBaseId,
        onEvent: ({ event, data }) => {
          if (event === "status") setStatusText(data.message);
          if (event === "answer") {
            setActiveId(data.conversation_id);
            setMessages((current) => appendStreamAnswer(current, data));
            if (data.answer?.answer_kind === "clarification") {
              setAnswerAnnouncement(`需要补充信息：${data.answer?.clarification?.question || "请回答澄清问题"}。`);
            } else {
              setAnswerAnnouncement(`AegisCopilot 回答已生成，共 ${Math.min(data.answer?.commands?.length || 0, 1)} 条命令。`);
            }
            if (data.memory_update) notifyMemoryUpdate(data);
          }
          if (event === "done" || event === "memory") notifyMemoryUpdate(data);
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

  async function executeCommand(command) {
    const api = window.aegisDesktop?.terminal;
    if (!api) {
      setError("当前运行环境没有可用的内置终端。");
      setTerminalOpen(true);
      return;
    }
    try {
      const sessions = await api.list();
      let session = sessions.find((item) => item.shell === command.shell);
      if (!session) session = await api.create({ shell: command.shell });
      const input = `${String(command.code || "").replace(/\r?\n/g, "\r")}\r`;
      await api.write({ terminalId: session.terminalId, data: input });
      setTerminalOpen(true);
    } catch (executionError) {
      setError(executionError.message || "无法向内置终端发送命令");
      setTerminalOpen(true);
    }
  }

  return <div className="app-frame">
    <div className="titlebar" data-ui="titlebar">
      <div className="titlebar-brand"><img src="./app-icon.png" alt="" /><span>AegisCopilot</span><small>v{appVersion}</small></div>
      <span className="titlebar-context">Developer Command RAG</span>
    </div>
    {view === "knowledge" ? (
      <KnowledgeManager
        knowledgeBases={knowledgeBases}
        initialId={selectedKnowledgeBaseId}
        onRefresh={refreshKnowledgeBases}
        onBack={() => setView("chat")}
        onSelectForChat={newConversation}
        onKnowledgeBaseDeleted={handleKnowledgeBaseDeleted}
      />
    ) : view === "profile" ? (
      <ProfileMemoryManager
        profile={profile}
        knowledgeBases={knowledgeBases}
        initialKnowledgeBaseId={selectedKnowledgeBaseId}
        refreshToken={memoryRevision}
        onBack={() => setView("chat")}
        onChanged={memoriesChanged}
      />
    ) : (
      <ChatWorkspace
        sidebarOpen={sidebarOpen}
        onSidebarOpenChange={setSidebarOpen}
        conversations={conversations}
        activeId={activeId}
        onNewConversation={() => newConversation()}
        onSelectConversation={selectConversation}
        onDeleteConversation={deleteConversation}
        onOpenKnowledge={() => setView("knowledge")}
        knowledgeBases={knowledgeBases}
        onOpenProfile={() => { setSettingsOpen(false); setMemoryDrawerOpen(false); setPersonalizationDrawerOpen(false); setView("profile"); }}
        profile={profile}
        onOpenSettings={() => { setMemoryDrawerOpen(false); setPersonalizationDrawerOpen(false); setSettingsOpen(true); }}
        onOpenTerminal={() => setTerminalOpen(true)}
        onExecuteCommand={executeCommand}
        modelStatus={modelStatus}
        activeConversation={activeConversation}
        selectedKnowledgeBaseId={selectedKnowledgeBaseId}
        onKnowledgeBaseChange={changeKnowledgeBase}
        knowledgeStatus={knowledge}
        messages={messages}
        answerAnnouncement={answerAnnouncement}
        busy={busy}
        statusText={statusText}
        error={error}
        onPreviewDocument={previewDocument}
        onOpenMemory={openMemory}
        onOpenPersonalization={openPersonalization}
        onSubmit={submit}
        query={query}
        onQueryChange={setQuery}
        composerRef={composerRef}
        bottomRef={bottomRef}
      />
    )}
    <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} modelStatus={modelStatus} onChanged={loadModelStatus} />
    <MemoryDrawer open={memoryDrawerOpen} onClose={() => setMemoryDrawerOpen(false)} memory={memoryState} loading={memoryLoading} error={memoryError} onReset={() => setMemoryResetOpen(true)} />
    <PersonalizationDrawer open={personalizationDrawerOpen} onClose={() => setPersonalizationDrawerOpen(false)} personalization={personalizationState} refreshToken={memoryRevision} />
    <DocumentPreviewModal preview={preview} onClose={() => setPreview(null)} />
    <TerminalPanel open={terminalOpen} onClose={() => setTerminalOpen(false)} />
    {memoryResetOpen && <Modal
      title="清空会话上下文"
      onClose={() => setMemoryResetOpen(false)}
      dataUi="memory-reset-modal"
      descriptionId="memory-reset-description"
      initialFocusRef={memoryResetCancelRef}
      actions={<><button ref={memoryResetCancelRef} className="secondary-button" type="button" onClick={() => setMemoryResetOpen(false)}>取消</button><button className="danger-button" type="button" onClick={resetMemory}>确认清空</button></>}
    ><p id="memory-reset-description">聊天记录会保留，但此前消息不会再用于后续回答。</p></Modal>}
    <ToastRegion
      toast={toast}
      onDismiss={dismissToast}
      onUndo={undoToast}
      onView={() => { dismissToast(); setSettingsOpen(false); setMemoryDrawerOpen(false); setPersonalizationDrawerOpen(false); setView("profile"); }}
    />
  </div>;
}
