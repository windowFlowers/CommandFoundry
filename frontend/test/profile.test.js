import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  categoryForScope,
  createProfileMemory,
  getMemoryExtractionTask,
  isTerminalMemoryExtractionStatus,
  memoryCategoriesForScope,
  memoriesForAnswer,
  memoryUpdateToast,
  normalizeMemoryUpdate,
  personalizationBadgeLabel,
  profileMemoryListPath,
  updateProfileMemory,
} from "../src/lib/profile.js";


const profileSource = await readFile(new URL("../src/components/profile/ProfileMemoryManager.jsx", import.meta.url), "utf8");
const drawerSource = await readFile(new URL("../src/components/overlays/PersonalizationDrawer.jsx", import.meta.url), "utf8");
const toastSource = await readFile(new URL("../src/components/ui/ToastRegion.jsx", import.meta.url), "utf8");
const appSource = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
const chatSource = await readFile(new URL("../src/components/chat/ChatWorkspace.jsx", import.meta.url), "utf8");
const knowledgeSource = await readFile(new URL("../src/components/knowledge/KnowledgeManager.jsx", import.meta.url), "utf8");
const profileHookSource = await readFile(new URL("../src/hooks/useProfileMemory.js", import.meta.url), "utf8");


test("keeps global and knowledge-base categories mutually valid", () => {
  assert.deepEqual(memoryCategoriesForScope("global").map((item) => item.value), ["response_style", "expertise"]);
  assert.deepEqual(memoryCategoriesForScope("knowledge_base").map((item) => item.value), ["platform", "toolchain", "project_constraint"]);
  assert.equal(categoryForScope("global", "platform"), "response_style");
  assert.equal(categoryForScope("knowledge_base", "response_style"), "platform");
  assert.equal(categoryForScope("knowledge_base", "toolchain"), "toolchain");
});


test("does not send UI-only all filters to the profile endpoint", () => {
  assert.equal(
    profileMemoryListPath({ scope: "all", category: "all", status: "all" }),
    "/profile/memories",
  );
  assert.equal(
    profileMemoryListPath({ scope: "knowledge_base", category: "platform", knowledgeBaseId: "developer-it", status: "active" }),
    "/profile/memories?scope=knowledge_base&category=platform&knowledge_base_id=developer-it&status=active",
  );
});


test("formats unique personalization badges and preserves answer memory order", () => {
  assert.equal(personalizationBadgeLabel({ personalization: { used: false, memory_ids: ["a"] } }), "");
  assert.equal(personalizationBadgeLabel({ personalization: { used: true, memory_ids: ["a", "a", "b"] } }), "个性化 · 2 条");
  assert.deepEqual(
    memoriesForAnswer([{ id: "b", value: "B" }], { memory_ids: ["a", "b", "a"] }),
    [{ id: "a", missing: true, display_text: "记忆已删除" }, { id: "b", value: "B" }],
  );
});


test("normalizes saved and queued memory updates without duplicate ids", () => {
  assert.deepEqual(normalizeMemoryUpdate({ memory_update: {
    status: "SAVED",
    source_message_id: "message-1",
    memory_ids: ["a", "a"],
    memories: [{ id: "b" }],
    operation: "add",
  } }), {
    status: "saved",
    sourceMessageId: "message-1",
    memoryIds: ["a", "b"],
    undoableMemoryIds: ["a", "b"],
    operation: "ADD",
    taskId: null,
  });
  assert.equal(normalizeMemoryUpdate({ conversation_id: "conversation-1" }), null);
});


test("uses the persisted extraction task endpoint and maps every terminal outcome", async () => {
  const calls = [];
  const request = async (...args) => { calls.push(args); return { status: "ready" }; };
  await getMemoryExtractionTask("task / 1", request);
  assert.deepEqual(calls, [["/profile/memory-extractions/task%20%2F%201"]]);

  const added = normalizeMemoryUpdate({
    id: "task-add",
    status: "ready",
    memory_ids: ["memory-a"],
    undoable_memory_ids: ["memory-a"],
    operation: "ADD",
  });
  assert.deepEqual(memoryUpdateToast(added), {
    message: "已记住 1 条",
    memoryIds: ["memory-a"],
    undoableMemoryIds: ["memory-a"],
    operation: "ADD",
  });
  assert.deepEqual(memoryUpdateToast(normalizeMemoryUpdate({ id: "task-noop", status: "ready", operation: "NOOP" })), {
    message: "未发现需要长期记住的信息",
    memoryIds: [],
    undoableMemoryIds: [],
    operation: "NOOP",
  });
  assert.deepEqual(memoryUpdateToast(normalizeMemoryUpdate({ id: "task-failed", status: "failed" })), {
    message: "记忆提取失败，未保存任何信息",
    memoryIds: [],
    undoableMemoryIds: [],
    operation: "ERROR",
  });
  assert.equal(memoryUpdateToast(normalizeMemoryUpdate({ status: "noop" })), null);
  assert.equal(isTerminalMemoryExtractionStatus("extracting"), false);
  assert.equal(isTerminalMemoryExtractionStatus("ready"), true);
});


test("uses the profile CRUD request contract", async () => {
  const calls = [];
  const request = async (...args) => { calls.push(args); return { id: "memory-1" }; };
  await createProfileMemory({ scope: "global" }, request);
  await updateProfileMemory("memory / 1", { pinned: true }, request);
  assert.deepEqual(calls, [
    ["/profile/memories", { method: "POST", body: { scope: "global" } }],
    ["/profile/memories/memory%20%2F%201", { method: "PATCH", body: { pinned: true } }],
  ]);
});


test("keeps create defaults valid and limits manually entered memory to 160 characters", () => {
  assert.match(profileSource, /scope: memory\?\.scope \|\| "global"/);
  assert.match(profileSource, /category: memory\?\.category \|\| "response_style"/);
  assert.match(profileSource, /maxLength=\{160\}/);
  assert.match(profileSource, /categoryForScope\(scope, current\.category\)/);
});


test("only sends backend-supported fields when editing an existing memory", () => {
  assert.match(profileSource, /const editable = \{\s+value: content,\s+display_text: content,\s+pinned: draft\.pinned,\s+\}/);
  assert.match(profileSource, /updateProfileMemory\(editor\.memory\.id, editable\)/);
  assert.match(profileSource, /memory \? \([\s\S]*profile-memory-editor-meta[\s\S]*\) : <>/);
});


test("bases forget-all controls on the unfiltered active profile count", () => {
  assert.match(profileSource, /const activeMemoryCount = localProfile\?\.active_memory_count/);
  assert.match(profileSource, /disabled=\{activeMemoryCount === 0\}/);
  assert.match(profileSource, /永久删除全部 \{activeMemoryCount\} 条长期记忆/);
  assert.doesNotMatch(profileSource, /disabled=\{total === 0\}/);
});


test("wires the profile page, answer badge, detail drawer, toast and undo flow", () => {
  assert.match(appSource, /<ProfileMemoryManager/);
  assert.match(appSource, /<PersonalizationDrawer/);
  assert.match(appSource, /<ToastRegion/);
  assert.match(chatSource, /data-ui="personalization-badge"/);
  assert.match(drawerSource, /listProfileMemories\(\{ ids \}\)/);
  assert.doesNotMatch(drawerSource, /status: "all"/);
  assert.match(toastSource, /toast\.undoableMemoryIds\?\.length > 0/);
  assert.match(toastSource, /onClick=\{onUndo\}/);
  assert.match(profileHookSource, /getMemoryExtractionTask\(update\.taskId\)/);
  assert.match(profileHookSource, /EXTRACTION_POLL_INTERVAL_MS = 2_000/);
  assert.doesNotMatch(profileHookSource, /attempt < 24/);
});


test("warns how many scoped memories a knowledge-base deletion removes", () => {
  assert.match(knowledgeSource, /deleteTarget\.item\.memory_count > 0/);
  assert.match(knowledgeSource, /同时删除 \$\{deleteTarget\.item\.memory_count\} 条知识库记忆/);
});
