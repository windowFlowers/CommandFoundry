import { fetchJson } from "./api.js";


export function contextBadgeLabel(answer) {
  const context = answer?.context || {};
  if (!context.used) return "";
  return context.summary_used
    ? `摘要 + ${context.recent_turn_count || 0}轮`
    : `上下文 · ${context.recent_turn_count || 0}轮`;
}


export function memoryStateForAnswer(memory, messages, context) {
  if (!memory || !context?.used) return memory;
  const usedIds = new Set(context.used_message_ids || []);
  const recentMessages = messages
    .filter((message) => usedIds.has(message.id))
    .map((message) => ({
      id: message.id,
      role: message.role,
      preview: message.role === "user" ? message.query : message.answer?.summary || "",
      created_at: message.created_at || null,
    }));
  return {
    ...memory,
    estimated_tokens: context.estimated_tokens || 0,
    recent_messages: recentMessages.length ? recentMessages : memory.recent_messages,
  };
}


export function appendStreamAnswer(messages, data) {
  const updated = [...messages];
  for (let index = updated.length - 1; index >= 0; index -= 1) {
    if (updated[index].role === "user" && String(updated[index].id).startsWith("local-")) {
      updated[index] = { ...updated[index], id: data.user_message_id || updated[index].id };
      break;
    }
  }
  updated.push({ id: data.message_id, role: "assistant", answer: data.answer });
  return updated;
}


export async function resetConversationMemory(conversationId, request = fetchJson) {
  return request(`/conversations/${conversationId}/memory/reset`, { method: "POST" });
}
