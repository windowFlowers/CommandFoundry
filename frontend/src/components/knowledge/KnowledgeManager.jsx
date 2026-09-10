import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  BookOpen,
  FileText,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  UploadCloud,
  WifiOff,
} from "lucide-react";
import { fetchJson } from "../../lib/api";
import { formatBytes, formatTime } from "../../lib/formatters";
import { Modal } from "../ui/Overlays";

const DEFAULT_KNOWLEDGE_BASE_ID = "developer-it";

export function KnowledgeManager({ knowledgeBases, initialId, onRefresh, onBack, onSelectForChat, onKnowledgeBaseDeleted }) {
  const [activeId, setActiveId] = useState(initialId || DEFAULT_KNOWLEDGE_BASE_ID);
  const [documents, setDocuments] = useState([]);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const [newName, setNewName] = useState("");
  const [renameName, setRenameName] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [reindexingId, setReindexingId] = useState(null);
  const [preview, setPreview] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const fileRef = useRef(null);
  const uploadZoneRef = useRef(null);
  const deleteCancelRef = useRef(null);
  const deleteReturnFocusRef = useRef(null);
  const deletePendingRef = useRef(false);
  const loadRevisionRef = useRef(0);
  const activeBase = knowledgeBases.find((item) => item.id === activeId) || knowledgeBases[0];

  async function load(baseId = activeId) {
    const revision = ++loadRevisionRef.current;
    try {
      const [docs, state] = await Promise.all([
        fetchJson(`/knowledge-bases/${baseId}/documents`),
        fetchJson(`/knowledge/status?knowledge_base_id=${encodeURIComponent(baseId)}`),
      ]);
      if (revision !== loadRevisionRef.current) return;
      setDocuments(docs.items); setStatus(state); setError("");
    } catch (loadError) {
      if (revision === loadRevisionRef.current) setError(loadError.message);
    }
  }

  function selectBase(baseId) {
    if (baseId === activeId) return;
    loadRevisionRef.current += 1;
    setDocuments([]);
    setStatus(null);
    setError("");
    setActiveId(baseId);
  }

  useEffect(() => { if (activeBase) load(activeBase.id); }, [activeId, knowledgeBases.length]);
  useEffect(() => {
    if (!documents.some((item) => item.status === "pending" || item.status === "indexing")) return undefined;
    const timer = window.setInterval(() => { load(); onRefresh(); }, 1200);
    return () => window.clearInterval(timer);
  }, [documents, activeId]);

  async function createBase(event) {
    event.preventDefault(); if (!newName.trim()) return;
    try {
      const created = await fetchJson("/knowledge-bases", { method: "POST", body: { name: newName.trim() } });
      setNewName(""); await onRefresh(); selectBase(created.id);
    } catch (actionError) { setError(actionError.message); }
  }

  async function renameBase(event) {
    event.preventDefault(); if (!renameName.trim()) return;
    try {
      await fetchJson(`/knowledge-bases/${activeId}`, { method: "PATCH", body: { name: renameName.trim() } });
      setRenaming(false); setRenameName(""); await onRefresh();
    } catch (actionError) { setError(actionError.message); }
  }

  async function upload(files) {
    const selected = Array.from(files || []); if (!selected.length) return;
    setUploading(true); setError("");
    let uploadError = "";
    try {
      for (const file of selected) {
        const form = new FormData();
        form.append("file", file);
        await fetchJson(`/knowledge-bases/${activeId}/documents`, { method: "POST", body: form });
      }
    } catch (actionError) { uploadError = actionError.message; }
    finally {
      await load();
      try { await onRefresh(); }
      catch (refreshError) { uploadError ||= refreshError.message; }
      if (uploadError) setError(uploadError);
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function reindexDocument(document) {
    if (reindexingId) return;
    setReindexingId(document.id);
    setError("");
    try {
      await fetchJson(`/knowledge-documents/${document.id}/reindex`, { method: "POST" });
      await load();
    } catch (actionError) { setError(actionError.message); }
    finally { setReindexingId(null); }
  }

  async function previewDocument(documentId) {
    try { setPreview(await fetchJson(`/knowledge-documents/${documentId}/content`)); }
    catch (actionError) { setError(actionError.message); }
  }

  async function deleteDocument(document) {
    if (deletePendingRef.current) return;
    deletePendingRef.current = true;
    setDeleting(true);
    setError("");
    try {
      await fetchJson(`/knowledge-documents/${document.id}`, { method: "DELETE" });
      deleteReturnFocusRef.current = uploadZoneRef.current;
      setDeleteTarget(null); await load(); await onRefresh();
    } catch (actionError) { setError(actionError.message); }
    finally { deletePendingRef.current = false; setDeleting(false); }
  }

  async function deleteBase(base) {
    if (deletePendingRef.current) return;
    deletePendingRef.current = true;
    setDeleting(true);
    setError("");
    try {
      await fetchJson(`/knowledge-bases/${base.id}`, { method: "DELETE" });
      deleteReturnFocusRef.current = uploadZoneRef.current;
      setDeleteTarget(null); selectBase(DEFAULT_KNOWLEDGE_BASE_ID); await onRefresh();
      await onKnowledgeBaseDeleted?.(base.id);
    } catch (actionError) { setError(actionError.message); }
    finally { deletePendingRef.current = false; setDeleting(false); }
  }

  function openDeleteModal(target) {
    deleteReturnFocusRef.current = null;
    setDeleteTarget(target);
  }

  function closeDeleteModal() {
    if (deletePendingRef.current) return;
    deleteReturnFocusRef.current = null;
    setDeleteTarget(null);
  }

  if (!activeBase) return null;
  return <main className="knowledge-page" data-ui="knowledge-page">
    <header className="knowledge-page-header">
      <button className="secondary-button back-button" type="button" onClick={onBack} data-ui="back-to-chat"><ArrowLeft size={16} />返回问答</button>
      <div><span className="eyebrow">KNOWLEDGE WORKSPACE</span><h1>知识库管理</h1></div>
      <span className="page-index">COLLECTION / {String(knowledgeBases.length).padStart(2, "0")}</span>
    </header>
    <div className="knowledge-layout">
      <aside className="library-panel">
        <div className="panel-title"><span>知识库</span><span>{String(knowledgeBases.length).padStart(2, "0")}</span></div>
        <nav aria-label="知识库" data-ui="knowledge-base-list">
          {knowledgeBases.map((base, index) => (
            <button key={base.id} className={base.id === activeId ? "active" : ""} type="button" onClick={() => selectBase(base.id)} aria-current={base.id === activeId ? "page" : undefined}>
              <span className="library-number">{String(index + 1).padStart(2, "0")}</span><BookOpen size={16} />
              <span><strong>{base.name}</strong><small>{base.is_builtin ? "内置" : `${base.document_count} 个文档`}</small></span>
            </button>
          ))}
        </nav>
        <form className="new-library" onSubmit={createBase} data-ui="new-library-form">
          <label className="sr-only" htmlFor="new-library-name">新知识库名称</label>
          <input id="new-library-name" value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="新知识库名称" maxLength={64} />
          <button type="submit" disabled={!newName.trim()} aria-label="创建知识库"><Plus size={16} /></button>
        </form>
      </aside>
      <section className="documents-panel">
        <header className="documents-head">
          <div>{renaming ? (
            <form className="rename-form" onSubmit={renameBase}>
              <label className="sr-only" htmlFor="rename-library">知识库名称</label>
              <input id="rename-library" autoFocus value={renameName} onChange={(event) => setRenameName(event.target.value)} />
              <button type="submit">保存</button><button type="button" onClick={() => setRenaming(false)}>取消</button>
            </form>
          ) : <><span className="eyebrow">ACTIVE COLLECTION</span><h2>{activeBase.name}</h2><div className="index-facts"><span>{status?.topic_count || 0} 个索引片段</span><span>{status?.retrieval_mode === "hybrid" ? "BM25 + BGE · RRF" : "BM25"}</span></div></>}</div>
          <div className="document-actions">
            <button type="button" onClick={() => onSelectForChat(activeBase.id)} data-ui="use-for-chat">用于新对话</button>
            {!activeBase.is_builtin && <>
              <button type="button" onClick={() => { setRenameName(activeBase.name); setRenaming(true); }}><Pencil size={15} />重命名</button>
              <button className="danger-link" type="button" onClick={() => openDeleteModal({ type: "base", item: activeBase })} data-ui="delete-knowledge-base"><Trash2 size={15} />删除</button>
            </>}
          </div>
        </header>
        <button
          ref={uploadZoneRef}
          className={`upload-zone ${dragging ? "dragging" : ""}`}
          type="button"
          disabled={uploading}
          onClick={() => fileRef.current?.click()}
          onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => { event.preventDefault(); setDragging(false); upload(event.dataTransfer.files); }}
          data-ui="upload-zone"
        >
          <span className="upload-index">DROP / IMPORT</span><UploadCloud size={24} /><strong>{uploading ? "正在上传" : "选择或拖入文档"}</strong><span>TXT · Markdown · PDF · DOCX · 最大 20 MB</span>
        </button>
        <input ref={fileRef} className="file-input" type="file" multiple accept=".txt,.md,.markdown,.pdf,.docx" onChange={(event) => upload(event.target.files)} />
        {error && <div className="error-banner" role="alert"><WifiOff size={17} /><span>{error}</span></div>}
        <div className="document-table" role="table" aria-label={`${activeBase.name}文档`} data-ui="document-table">
          <div className="document-table-head" role="row"><span role="columnheader">文档</span><span role="columnheader">状态</span><span role="columnheader">来源</span><span role="columnheader">大小</span><span role="columnheader">更新时间</span><span role="columnheader">操作</span></div>
          {documents.length === 0 ? <div className="empty-documents" role="row"><span role="cell"><FileText size={24} />还没有上传文档</span></div> : documents.map((document, index) => (
            <div className="document-row" key={document.id} role="row" data-ui="document-row">
              <div role="cell"><span className="document-number">{String(index + 1).padStart(2, "0")}</span><FileText size={17} /><span><strong>{document.filename}</strong>{document.error && <small>{document.error}</small>}</span></div>
              <span className={`document-status ${document.status}`} role="cell">{document.status === "ready" ? `${document.chunk_count} 片段` : document.status === "failed" ? "失败" : document.status === "indexing" ? "索引中" : "等待中"}</span>
              <span role="cell">{document.source_kind === "upload" ? "本地上传" : document.source_kind}</span>
              <span role="cell">{formatBytes(document.size_bytes)}</span><span role="cell">{formatTime(document.updated_at)}</span>
              <div role="cell">
                <button type="button" disabled={document.status !== "ready"} onClick={() => previewDocument(document.id)} data-ui="preview-document">预览</button>
                <button type="button" disabled={Boolean(reindexingId)} aria-busy={reindexingId === document.id} onClick={() => reindexDocument(document)} aria-label={`重新索引 ${document.filename}`}><RefreshCw className={reindexingId === document.id ? "spin" : ""} size={14} /></button>
                <button type="button" onClick={() => openDeleteModal({ type: "document", item: document })} aria-label={`删除文档 ${document.filename}`} data-ui="delete-document"><Trash2 size={14} /></button>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
    {preview && <Modal title={preview.filename} onClose={() => setPreview(null)} dataUi="document-preview-modal"><pre className="document-preview">{preview.content}</pre></Modal>}
    {deleteTarget && <Modal
      title={deleteTarget.type === "base" ? "删除知识库" : "删除文档"}
      onClose={closeDeleteModal}
      dataUi="delete-confirm-modal"
      descriptionId="delete-confirm-description"
      initialFocusRef={deleteCancelRef}
      returnFocusRef={deleteReturnFocusRef}
      closeDisabled={deleting}
      actions={<><button ref={deleting ? null : deleteCancelRef} className="secondary-button" type="button" onClick={closeDeleteModal} disabled={deleting}>取消</button><button className="danger-button" type="button" disabled={deleting} aria-busy={deleting} onClick={() => deleteTarget.type === "base" ? deleteBase(deleteTarget.item) : deleteDocument(deleteTarget.item)}>{deleting ? "正在删除…" : "确认删除"}</button></>}
    ><p id="delete-confirm-description">将删除“{deleteTarget.item.name || deleteTarget.item.filename}”及其本地索引。此操作不能撤销。</p><span className="sr-only" role="status" aria-live="polite">{deleting ? "正在删除，请稍候。" : ""}</span></Modal>}
  </main>;
}
