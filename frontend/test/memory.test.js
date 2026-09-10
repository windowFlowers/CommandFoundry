import assert from "node:assert/strict";
import test from "node:test";

import {
  appendStreamAnswer,
  contextBadgeLabel,
  memoryStateForAnswer,
  resetConversationMemory,
} from "../src/lib/memory.js";


test("formats recent and summarized context labels", () => {
  assert.equal(contextBadgeLabel({ context: { used: false } }), "");
  assert.equal(
    contextBadgeLabel({ context: { used: true, summary_used: false, recent_turn_count: 2 } }),
    "上下文 · 2轮",
  );
  assert.equal(
    contextBadgeLabel({ context: { used: true, summary_used: true, recent_turn_count: 2 } }),
    "摘要 + 2轮",
  );
});


test("shows history for the clicked answer instead of the latest answer", () => {
  const memory = { estimated_tokens: 999, recent_messages: [{ id: "newer" }] };
  const messages = [
    { id: "u1", role: "user", query: "Linux 怎么查看端口？" },
    { id: "a1", role: "assistant", answer: { summary: "使用 ss。" } },
    { id: "u2", role: "user", query: "那 Windows 呢？" },
  ];
  const selected = memoryStateForAnswer(memory, messages, {
    used: true,
    used_message_ids: ["u1", "a1"],
    estimated_tokens: 42,
  });
  assert.equal(selected.estimated_tokens, 42);
  assert.deepEqual(selected.recent_messages.map((message) => message.id), ["u1", "a1"]);
  assert.equal(selected.recent_messages[1].preview, "使用 ss。");
});


test("reset uses the conversation-scoped memory endpoint", async () => {
  const calls = [];
  await resetConversationMemory("conversation-1", async (...args) => {
    calls.push(args);
    return null;
  });
  assert.deepEqual(calls, [
    ["/conversations/conversation-1/memory/reset", { method: "POST" }],
  ]);
});


test("replaces optimistic user ids with persisted ids from the stream", () => {
  const messages = appendStreamAnswer(
    [{ id: "local-1", role: "user", query: "问题" }],
    { user_message_id: "user-1", message_id: "assistant-1", answer: { summary: "回答" } },
  );
  assert.deepEqual(messages.map((message) => message.id), ["user-1", "assistant-1"]);
});
