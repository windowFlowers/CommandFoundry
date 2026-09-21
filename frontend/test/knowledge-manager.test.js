import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/components/knowledge/KnowledgeManager.jsx", import.meta.url), "utf8");
const appSource = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
const chatSource = await readFile(new URL("../src/components/chat/ChatWorkspace.jsx", import.meta.url), "utf8");
const drawersSource = await readFile(new URL("../src/components/overlays/AppDrawers.jsx", import.meta.url), "utf8");


test("locks every delete-modal close path while the DELETE request is pending", () => {
  assert.match(source, /const deletePendingRef = useRef\(false\)/);
  assert.match(source, /function closeDeleteModal\(\) \{\s+if \(deletePendingRef\.current\) return;/);
  assert.match(source, /onClose=\{closeDeleteModal\}/);
  assert.match(source, /onClick=\{closeDeleteModal\} disabled=\{deleting\}/);
});


test("prevents duplicate deletes and exposes an accessible pending state", () => {
  assert.equal(source.match(/if \(deletePendingRef\.current\) return;/g)?.length, 3);
  assert.equal(source.match(/disabled=\{deleting\}/g)?.length, 2);
  assert.match(source, /aria-busy=\{deleting\}/);
  assert.match(source, /deleting \? "正在删除…" : "确认删除"/);
  assert.match(source, /role="status" aria-live="polite"/);
});


test("selects the stable upload zone before closing after a successful delete", () => {
  const focusAssignment = "deleteReturnFocusRef.current = uploadZoneRef.current;";
  const closeModal = "setDeleteTarget(null);";
  const documentDelete = source.indexOf("async function deleteDocument");
  const baseDelete = source.indexOf("async function deleteBase");

  assert.ok(source.indexOf(focusAssignment, documentDelete) < source.indexOf(closeModal, documentDelete));
  assert.ok(source.indexOf(focusAssignment, baseDelete) < source.indexOf(closeModal, baseDelete));
});


test("synchronizes parent state after deleting a knowledge base", () => {
  assert.match(source, /await onKnowledgeBaseDeleted\?\.\(base\.id\)/);
  assert.match(appSource, /async function handleKnowledgeBaseDeleted\(deletedId\)/);
  assert.match(appSource, /knowledgeBases\.some\(\(base\) => base\.id === baseId\)/);
  assert.match(chatSource, /activeConversation\.knowledge_base_name}（已删除）/);
});


test("keeps the settings drawer free of explanatory helper copy", () => {
  assert.doesNotMatch(drawersSource, /field-help/);
  assert.doesNotMatch(drawersSource, /不会写入项目文件/);
});


test("rejects stale knowledge-base loads and refreshes after partial uploads", () => {
  assert.match(source, /const loadRevisionRef = useRef\(0\)/);
  assert.match(source, /if \(revision !== loadRevisionRef\.current\) return/);
  assert.match(source, /finally \{\s+await load\(\)/);
  assert.match(source, /async function reindexDocument\(document\)[\s\S]*catch \(actionError\)/);
});


test("shows the source of each uploaded document", () => {
  assert.match(source, /<span role="columnheader">来源<\/span>/);
  assert.match(source, /document\.source_kind === "upload" \? "本地上传"/);
});


test("renders deterministic cleaning quality without exposing document content", () => {
  assert.match(source, /export function DocumentQualitySummary\(\{ document \}\)/);
  assert.match(source, /document\?\.quality_report \|\| document\?\.quality/);
  assert.match(source, /data-ui="document-quality"/);
  assert.match(source, /data-quality-status=\{status \|\| "unknown"\}/);
  assert.match(source, /cleaned_content_sha256/);
  assert.match(source, /removed_headers_footers/);
  assert.match(source, /removed_exact_duplicates/);
  assert.match(source, /removed_near_duplicates/);
  assert.match(source, /report\.warnings\.length/);
  assert.match(source, /DocumentQualitySummary document=\{document\}/);
  assert.doesNotMatch(source, /document\.raw_content/);
  assert.doesNotMatch(source, /document\.original_content/);
});


test("keeps quality states backwards compatible when older document responses omit a report", () => {
  assert.match(source, /if \(!hasReport\) return null/);
  assert.match(source, /清洗通过/);
  assert.match(source, /清洗有警告/);
  assert.match(source, /清洗已拒绝/);
});
