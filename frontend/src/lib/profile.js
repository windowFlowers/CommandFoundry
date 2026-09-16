import { fetchJson } from "./api.js";


export const memoryScopeLabels = {
  global: "全局",
  knowledge_base: "知识库",
};

export const memoryCategoryLabels = {
  response_style: "答复风格",
  expertise: "熟练度",
  platform: "运行平台",
  toolchain: "工具链",
  project_constraint: "项目约束",
};

export const memoryCategoriesByScope = {
  global: ["response_style", "expertise"],
  knowledge_base: ["platform", "toolchain", "project_constraint"],
};


export function memoryCategoriesForScope(scope) {
  const categories = memoryCategoriesByScope[scope] || Object.keys(memoryCategoryLabels);
  return categories.map((value) => ({ value, label: memoryCategoryLabels[value] }));
}


export function categoryForScope(scope, category) {
  const categories = memoryCategoriesByScope[scope] || [];
  return categories.includes(category) ? category : categories[0] || "response_style";
}


export function personalizationBadgeLabel(answer) {
  const personalization = answer?.personalization || {};
  const count = new Set(personalization.memory_ids || []).size;
  return personalization.used && count > 0 ? `个性化 · ${count} 条` : "";
}


export function profileMemoryListPath(filters = {}) {
  const params = new URLSearchParams();
  const entries = {
    q: filters.q,
    scope: filters.scope === "all" ? "" : filters.scope,
    category: filters.category === "all" ? "" : filters.category,
    knowledge_base_id: filters.knowledgeBaseId || filters.knowledge_base_id,
    status: filters.status === "all" ? "" : filters.status,
    ids: Array.isArray(filters.ids) ? filters.ids.join(",") : filters.ids,
    source_message_id: filters.sourceMessageId || filters.source_message_id,
  };
  Object.entries(entries).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value).trim()) params.set(key, String(value).trim());
  });
  const query = params.toString();
  return `/profile/memories${query ? `?${query}` : ""}`;
}


export function memoriesForAnswer(items, personalization) {
  const byId = new Map((items || []).map((item) => [item.id, item]));
  return [...new Set(personalization?.memory_ids || [])].map((id) => byId.get(id) || {
    id,
    missing: true,
    display_text: "记忆已删除",
  });
}


export function normalizeMemoryUpdate(payload) {
  const source = payload?.memory_update || payload;
  if (!source || typeof source !== "object") return null;
  const memoryIds = [...new Set([
    ...(Array.isArray(source.memory_ids) ? source.memory_ids : []),
    ...(Array.isArray(source.memories) ? source.memories.map((item) => item?.id) : []),
  ].filter(Boolean))];
  const operation = String(source.operation || "ADD").toUpperCase();
  const explicitUndoableIds = Array.isArray(source.undoable_memory_ids)
    ? source.undoable_memory_ids
    : null;
  const undoableMemoryIds = [...new Set((explicitUndoableIds || (operation === "ADD" ? memoryIds : []))
    .filter(Boolean))];
  const status = String(source.status || "").toLowerCase();
  if (!status && !memoryIds.length) return null;
  return {
    status,
    sourceMessageId: source.source_message_id || null,
    memoryIds,
    undoableMemoryIds,
    operation,
    taskId: source.task_id || source.id || null,
  };
}


export function isTerminalMemoryExtractionStatus(status) {
  return ["ready", "complete", "failed", "cancelled"].includes(String(status || "").toLowerCase());
}


export function memoryUpdateToast(update) {
  if (!update) return null;
  const status = String(update.status || "").toLowerCase();
  const memoryIds = [...new Set(update.memoryIds || [])];
  const undoableMemoryIds = [...new Set(update.undoableMemoryIds || [])];
  const operation = String(update.operation || "NOOP").toUpperCase();

  if (status === "failed") {
    return {
      message: "记忆提取失败，未保存任何信息",
      memoryIds: [],
      undoableMemoryIds: [],
      operation: "ERROR",
    };
  }
  if (status === "cancelled") {
    return {
      message: "记忆提取已取消",
      memoryIds: [],
      undoableMemoryIds: [],
      operation: "CANCELLED",
    };
  }
  // Synchronous no-ops are emitted for ordinary questions and should remain
  // quiet. A queued model task that finishes with no candidates is different:
  // it deserves an explicit, non-error outcome.
  if ((status === "ready" || status === "complete") && memoryIds.length === 0) {
    return {
      message: "未发现需要长期记住的信息",
      memoryIds: [],
      undoableMemoryIds: [],
      operation: "NOOP",
    };
  }
  if (!["saved", "ready", "complete"].includes(status) || memoryIds.length === 0) return null;
  const message = operation === "ADD"
    ? `已记住 ${memoryIds.length} 条`
    : operation === "SUPERSEDE"
      ? `已替换 ${memoryIds.length} 条记忆`
      : `已更新 ${memoryIds.length} 条记忆`;
  return { message, memoryIds, undoableMemoryIds, operation };
}


export async function getProfile(request = fetchJson) {
  return request("/profile");
}


export async function updateProfile(patch, request = fetchJson) {
  return request("/profile", { method: "PATCH", body: patch });
}


export async function listProfileMemories(filters = {}, request = fetchJson) {
  return request(profileMemoryListPath(filters));
}


export async function getMemoryExtractionTask(taskId, request = fetchJson) {
  return request(`/profile/memory-extractions/${encodeURIComponent(taskId)}`);
}


export async function createProfileMemory(memory, request = fetchJson) {
  return request("/profile/memories", { method: "POST", body: memory });
}


export async function updateProfileMemory(memoryId, patch, request = fetchJson) {
  return request(`/profile/memories/${encodeURIComponent(memoryId)}`, { method: "PATCH", body: patch });
}


export async function deleteProfileMemory(memoryId, request = fetchJson) {
  return request(`/profile/memories/${encodeURIComponent(memoryId)}`, { method: "DELETE" });
}


export async function undoProfileMemory(memoryId, request = fetchJson) {
  return request(`/profile/memories/${encodeURIComponent(memoryId)}/undo`, { method: "POST" });
}


export async function resetProfileMemories(request = fetchJson) {
  return request("/profile/memories/reset", { method: "POST" });
}
