import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  BrainCircuit,
  Pencil,
  Pin,
  Plus,
  Search,
  Trash2,
  WifiOff,
} from "lucide-react";
import { formatTime } from "../../lib/formatters";
import {
  categoryForScope,
  createProfileMemory,
  deleteProfileMemory,
  listProfileMemories,
  memoryCategoriesByScope,
  memoryCategoriesForScope,
  memoryCategoryLabels,
  memoryScopeLabels,
  resetProfileMemories,
  updateProfile,
  updateProfileMemory,
} from "../../lib/profile";
import { Modal } from "../ui/Overlays";
import { SelectMenu } from "../ui/SelectMenu";


const DEFAULT_KNOWLEDGE_BASE_ID = "developer-it";
const providerLabels = {
  local: "本地提取",
  deepseek: "模型提取",
  openai: "模型提取",
  qwen: "模型提取",
  moonshot: "模型提取",
  siliconflow: "模型提取",
  ollama: "模型提取",
  custom: "模型提取",
  model: "模型提取",
  manual: "手动添加",
};


function memoryKnowledgeBaseName(memory, knowledgeBases) {
  if (memory.scope !== "knowledge_base") return "全部知识库";
  return memory.knowledge_base_name
    || knowledgeBases.find((base) => base.id === memory.knowledge_base_id)?.name
    || "知识库已删除";
}


function MemoryEditorModal({ memory, knowledgeBases, defaultKnowledgeBaseId, working, onClose, onSave }) {
  const contentRef = useRef(null);
  const [draft, setDraft] = useState(() => ({
    scope: memory?.scope || "global",
    knowledgeBaseId: memory?.knowledge_base_id || defaultKnowledgeBaseId || DEFAULT_KNOWLEDGE_BASE_ID,
    category: memory?.category || "response_style",
    content: memory?.display_text || memory?.value || "",
    pinned: Boolean(memory?.pinned),
  }));
  const categories = memoryCategoriesForScope(draft.scope);
  const isValid = Boolean(draft.content.trim()) && (draft.scope !== "knowledge_base" || Boolean(draft.knowledgeBaseId));

  function changeScope(scope) {
    setDraft((current) => ({
      ...current,
      scope,
      category: categoryForScope(scope, current.category),
    }));
  }

  return (
    <Modal
      title={memory ? "编辑记忆" : "新增记忆"}
      onClose={onClose}
      dataUi="profile-memory-editor"
      initialFocusRef={contentRef}
      closeDisabled={working}
      actions={<>
        <button className="secondary-button" type="button" onClick={onClose} disabled={working}>取消</button>
        <button className="primary-button" type="button" onClick={() => onSave(draft)} disabled={working || !isValid}>{working ? "保存中" : "保存"}</button>
      </>}
    >
      <form className="profile-memory-form" onSubmit={(event) => { event.preventDefault(); if (isValid && !working) onSave(draft); }}>
        <label><span>记忆内容</span><textarea ref={contentRef} value={draft.content} onChange={(event) => setDraft((current) => ({ ...current, content: event.target.value }))} rows={4} maxLength={160} /></label>
        {memory ? (
          <div className="profile-memory-editor-meta" aria-label="记忆范围与类别">
            <span>{memoryScopeLabels[memory.scope] || memory.scope}</span>
            <span>{memoryCategoryLabels[memory.category] || memory.category}</span>
            {memory.scope === "knowledge_base" && <span>{memoryKnowledgeBaseName(memory, knowledgeBases)}</span>}
          </div>
        ) : <>
          <div className="profile-memory-form-row">
            <label><span>范围</span><SelectMenu value={draft.scope} onChange={changeScope} ariaLabel="范围" options={[{ value: "global", label: "全局" }, { value: "knowledge_base", label: "知识库" }]} /></label>
            <label><span>类别</span><SelectMenu value={draft.category} onChange={(category) => setDraft((current) => ({ ...current, category }))} ariaLabel="类别" options={categories} /></label>
          </div>
          {draft.scope === "knowledge_base" && <label><span>知识库</span><SelectMenu value={draft.knowledgeBaseId} onChange={(knowledgeBaseId) => setDraft((current) => ({ ...current, knowledgeBaseId }))} ariaLabel="知识库" options={knowledgeBases.map((base) => ({ value: base.id, label: base.name }))} /></label>}
        </>}
        <label className="profile-pin-field"><input type="checkbox" checked={draft.pinned} onChange={(event) => setDraft((current) => ({ ...current, pinned: event.target.checked }))} /><span>固定记忆</span></label>
      </form>
    </Modal>
  );
}


export function ProfileMemoryManager({ profile, knowledgeBases, initialKnowledgeBaseId, refreshToken = 0, onBack, onChanged }) {
  const [localProfile, setLocalProfile] = useState(profile);
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState({ q: "", scope: "all", category: "all", knowledgeBaseId: initialKnowledgeBaseId || DEFAULT_KNOWLEDGE_BASE_ID });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [profileWorking, setProfileWorking] = useState(false);
  const [editor, setEditor] = useState(null);
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [clearOpen, setClearOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const loadRevisionRef = useRef(0);
  const mutationPendingRef = useRef(false);
  const deleteCancelRef = useRef(null);
  const addButtonRef = useRef(null);
  const deleteReturnFocusRef = useRef(null);
  const backButtonRef = useRef(null);

  const activeMemoryCount = localProfile?.active_memory_count ?? profile?.active_memory_count ?? total;
  const filterCategories = memoryCategoriesForScope(filters.scope);

  useEffect(() => { if (profile) setLocalProfile(profile); }, [profile]);
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => backButtonRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, []);

  async function load(nextFilters = filters) {
    const revision = ++loadRevisionRef.current;
    setLoading(true);
    try {
      const payload = await listProfileMemories({
        q: nextFilters.q,
        scope: nextFilters.scope,
        category: nextFilters.category,
        knowledgeBaseId: nextFilters.scope === "knowledge_base" ? nextFilters.knowledgeBaseId : "",
        status: "active",
      });
      if (revision !== loadRevisionRef.current) return;
      setItems(payload?.items || []);
      setTotal(payload?.total ?? payload?.items?.length ?? 0);
      setError("");
    } catch (loadError) {
      if (revision === loadRevisionRef.current) setError(loadError.message || "无法读取记忆");
    } finally {
      if (revision === loadRevisionRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    const timer = window.setTimeout(() => { load(filters); }, filters.q ? 180 : 0);
    return () => window.clearTimeout(timer);
  }, [filters.q, filters.scope, filters.category, filters.knowledgeBaseId, refreshToken]);

  async function toggleProfile(field) {
    if (profileWorking) return;
    const previous = localProfile;
    const nextValue = !Boolean(localProfile?.[field]);
    setLocalProfile((current) => ({ ...(current || {}), [field]: nextValue }));
    setProfileWorking(true); setError("");
    try {
      const updated = await updateProfile({ [field]: nextValue });
      setLocalProfile(updated);
      await onChanged?.();
    } catch (actionError) {
      setLocalProfile(previous);
      setError(actionError.message || "无法更新个性化设置");
    } finally { setProfileWorking(false); }
  }

  async function saveMemory(draft) {
    if (saving) return;
    setSaving(true); setError("");
    const content = draft.content.trim();
    const editable = {
      value: content,
      display_text: content,
      pinned: draft.pinned,
    };
    try {
      if (editor?.memory) {
        await updateProfileMemory(editor.memory.id, editable);
      } else {
        await createProfileMemory({
          ...editable,
          scope: draft.scope,
          knowledge_base_id: draft.scope === "knowledge_base" ? draft.knowledgeBaseId : null,
          category: draft.category,
          key: `manual.${draft.category}.${Date.now().toString(36)}`,
        });
      }
      setEditor(null);
      await load();
      await onChanged?.();
    } catch (actionError) { setError(actionError.message || "无法保存记忆"); }
    finally { setSaving(false); }
  }

  async function togglePinned(memory) {
    try {
      await updateProfileMemory(memory.id, { pinned: !memory.pinned });
      await load();
      await onChanged?.();
    } catch (actionError) { setError(actionError.message || "无法更新记忆"); }
  }

  function closeDeleteModal() {
    if (mutationPendingRef.current) return;
    setDeleteTarget(null); setClearOpen(false); deleteReturnFocusRef.current = null;
  }

  async function deleteMemory() {
    if (!deleteTarget || mutationPendingRef.current) return;
    mutationPendingRef.current = true; setDeleting(true); setError("");
    try {
      await deleteProfileMemory(deleteTarget.id);
      deleteReturnFocusRef.current = addButtonRef.current;
      setDeleteTarget(null);
      await load();
      await onChanged?.();
    } catch (actionError) { setError(actionError.message || "无法删除记忆"); }
    finally { mutationPendingRef.current = false; setDeleting(false); }
  }

  async function clearMemories() {
    if (mutationPendingRef.current) return;
    mutationPendingRef.current = true; setDeleting(true); setError("");
    try {
      await resetProfileMemories();
      deleteReturnFocusRef.current = addButtonRef.current;
      setClearOpen(false);
      await load();
      await onChanged?.();
    } catch (actionError) { setError(actionError.message || "无法清空记忆"); }
    finally { mutationPendingRef.current = false; setDeleting(false); }
  }

  function changeFilterScope(scope) {
    setFilters((current) => ({
      ...current,
      scope,
      category: current.category === "all" || (memoryCategoriesByScope[scope] || []).includes(current.category)
        ? current.category
        : "all",
    }));
  }

  return <main className="knowledge-page profile-page" data-ui="profile-page">
    <header className="knowledge-page-header profile-page-header">
      <button ref={backButtonRef} className="secondary-button back-button" type="button" onClick={onBack} data-ui="back-to-chat"><ArrowLeft size={16} />返回问答</button>
      <div><span className="eyebrow">MEMORY WORKSPACE</span><h1>我的记忆</h1></div>
      <span className="page-index">ACTIVE / {String(activeMemoryCount).padStart(2, "0")}</span>
    </header>
    <div className="profile-page-body">
      <section className="profile-control-bar" aria-label="个性化状态">
        <div><BrainCircuit size={19} /><strong>个性化记忆</strong><span>{activeMemoryCount} 条</span></div>
        <div className="profile-switches">
          <button className="profile-switch" type="button" role="switch" aria-checked={Boolean(localProfile?.personalization_enabled)} disabled={profileWorking} onClick={() => toggleProfile("personalization_enabled")}><span aria-hidden="true" /><strong>使用个性化</strong></button>
          <button className="profile-switch" type="button" role="switch" aria-checked={Boolean(localProfile?.auto_memory_enabled)} disabled={profileWorking} onClick={() => toggleProfile("auto_memory_enabled")}><span aria-hidden="true" /><strong>自动记忆</strong></button>
        </div>
      </section>
      <section className="profile-memory-toolbar" aria-label="记忆筛选">
        <label className="profile-search"><span className="sr-only">搜索记忆</span><Search size={16} /><input value={filters.q} onChange={(event) => setFilters((current) => ({ ...current, q: event.target.value }))} placeholder="搜索记忆" /></label>
        <label><span className="sr-only">范围</span><SelectMenu value={filters.scope} onChange={changeFilterScope} ariaLabel="范围筛选" dataUi="profile-scope-filter" options={[{ value: "all", label: "全部范围" }, { value: "global", label: "全局" }, { value: "knowledge_base", label: "知识库" }]} /></label>
        <label><span className="sr-only">类别</span><SelectMenu value={filters.category} onChange={(category) => setFilters((current) => ({ ...current, category }))} ariaLabel="类别筛选" dataUi="profile-category-filter" options={[{ value: "all", label: "全部类别" }, ...filterCategories]} /></label>
        {filters.scope === "knowledge_base" && <label><span className="sr-only">知识库</span><SelectMenu value={filters.knowledgeBaseId} onChange={(knowledgeBaseId) => setFilters((current) => ({ ...current, knowledgeBaseId }))} ariaLabel="知识库筛选" options={knowledgeBases.map((base) => ({ value: base.id, label: base.name }))} /></label>}
        <span className="profile-toolbar-spacer" />
        <button ref={addButtonRef} className="primary-button" type="button" onClick={() => setEditor({ memory: null })} data-ui="add-profile-memory"><Plus size={15} />新增记忆</button>
        <button className="secondary-button profile-clear-button" type="button" onClick={() => setClearOpen(true)} disabled={activeMemoryCount === 0} data-ui="clear-profile-memories"><Trash2 size={15} />忘记全部</button>
      </section>
      {error && <div className="error-banner profile-page-error" role="alert"><WifiOff size={17} /><span>{error}</span></div>}
      <section className="profile-memory-ledger" aria-label="长期记忆" aria-busy={loading} data-ui="profile-memory-list">
        <div className="profile-memory-ledger-head" aria-hidden="true"><span>记忆</span><span>范围 / 类别</span><span>置信度</span><span>使用</span><span>更新时间</span><span>操作</span></div>
        {loading && items.length === 0 ? <div className="profile-memory-empty">正在读取</div> : items.length === 0 ? <div className="profile-memory-empty">没有匹配的记忆</div> : items.map((memory, index) => (
          <article className={`profile-memory-row ${memory.pinned ? "pinned" : ""}`} key={memory.id} data-ui="profile-memory-row">
            <div className="profile-memory-value"><span>{String(index + 1).padStart(2, "0")}</span><strong>{memory.display_text || memory.value}</strong><div><span>{providerLabels[memory.extraction_provider] || memory.extraction_provider}</span>{memory.source_message_id && <span>消息 {memory.source_message_id.slice(0, 8)}</span>}</div></div>
            <div className="profile-memory-scope"><strong>{memoryKnowledgeBaseName(memory, knowledgeBases)}</strong><span>{memoryScopeLabels[memory.scope] || memory.scope} · {memoryCategoryLabels[memory.category] || memory.category}</span></div>
            <span>{Math.round((memory.confidence ?? 1) * 100)}%</span>
            <span>{memory.use_count || 0} 次</span>
            <time dateTime={memory.updated_at || ""}>{formatTime(memory.updated_at)}</time>
            <div className="profile-memory-actions">
              <button type="button" aria-pressed={Boolean(memory.pinned)} onClick={() => togglePinned(memory)} aria-label={memory.pinned ? `取消固定 ${memory.display_text}` : `固定 ${memory.display_text}`}><Pin size={14} /></button>
              <button type="button" onClick={() => setEditor({ memory })} aria-label={`编辑 ${memory.display_text}`}><Pencil size={14} /></button>
              <button type="button" onClick={() => { deleteReturnFocusRef.current = null; setDeleteTarget(memory); }} aria-label={`删除 ${memory.display_text}`}><Trash2 size={14} /></button>
            </div>
          </article>
        ))}
      </section>
    </div>
    {editor && <MemoryEditorModal memory={editor.memory} knowledgeBases={knowledgeBases} defaultKnowledgeBaseId={filters.knowledgeBaseId} working={saving} onClose={() => { if (!saving) setEditor(null); }} onSave={saveMemory} />}
    {deleteTarget && <Modal title="删除记忆" onClose={closeDeleteModal} dataUi="profile-memory-delete" initialFocusRef={deleteCancelRef} returnFocusRef={deleteReturnFocusRef} closeDisabled={deleting} actions={<><button ref={deleting ? null : deleteCancelRef} className="secondary-button" type="button" disabled={deleting} onClick={closeDeleteModal}>取消</button><button className="danger-button" type="button" disabled={deleting} aria-busy={deleting} onClick={deleteMemory}>{deleting ? "正在删除" : "确认删除"}</button></>}><p>将删除“{deleteTarget.display_text || deleteTarget.value}”。此操作不能撤销。</p></Modal>}
    {clearOpen && <Modal title="忘记全部" onClose={closeDeleteModal} dataUi="profile-memory-reset" initialFocusRef={deleteCancelRef} returnFocusRef={deleteReturnFocusRef} closeDisabled={deleting} actions={<><button ref={deleting ? null : deleteCancelRef} className="secondary-button" type="button" disabled={deleting} onClick={closeDeleteModal}>取消</button><button className="danger-button" type="button" disabled={deleting} aria-busy={deleting} onClick={clearMemories}>{deleting ? "正在清空" : "确认清空"}</button></>}><p>将永久删除全部 {activeMemoryCount} 条长期记忆。</p></Modal>}
  </main>;
}
