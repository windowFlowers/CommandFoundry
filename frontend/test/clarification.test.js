import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const appSource = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
const chatSource = await readFile(new URL("../src/components/chat/ChatWorkspace.jsx", import.meta.url), "utf8");
const styleSource = await readFile(new URL("../src/styles-next.css", import.meta.url), "utf8");

test("renders clarification answers with one-at-a-time input and quick options", () => {
  assert.match(chatSource, /function ClarificationCard/);
  assert.match(chatSource, /clarification\?\.question \|\| "请补充信息"/);
  assert.match(chatSource, /clarification-options.*role="group"/s);
  assert.match(chatSource, /onClick=\{\(\) => submitValue\(option\.value\)\}/);
  assert.match(chatSource, /clarification-input-row/);
  assert.match(chatSource, /onSubmit\?\.\(normalized\)/);
  assert.match(chatSource, /给我一个模板示例/);
  assert.match(chatSource, /clarification-template/);
});

test("keeps stale clarification cards and controls inaccessible after the next turn", () => {
  assert.match(chatSource, /const latestClarificationId = lastMessage\?\.role === "assistant" && lastMessage\.answer\?\.answer_kind === "clarification"/);
  assert.match(chatSource, /interactive=\{message\.id === latestClarificationId\}/);
  assert.match(chatSource, /const locked = disabled \|\| stale/);
  assert.match(chatSource, /disabled=\{locked\}/);
  assert.match(chatSource, /已进入下一步，不能重复提交此问题/);
});

test("announces clarification prompts separately from generated commands", () => {
  assert.match(appSource, /data\.answer\?\.answer_kind === "clarification"/);
  assert.match(appSource, /需要补充信息：\$\{data\.answer\?\.clarification\?\.question/);
  assert.match(appSource, /Math\.min\(data\.answer\?\.commands\?\.length \|\| 0, 1\)/);
});

test("marks template fallback and keeps displayed output to one copyable block", () => {
  assert.match(chatSource, /const answerKind = answer\.answer_kind === "clarification" \? "clarification" : answer\.answer_kind === "direct" \? "direct" : "template"/);
  assert.match(chatSource, /const visibleCommands = \(answer\.commands \|\| \[\]\)\.slice\(0, 1\)/);
  assert.match(chatSource, /data-ui="template-note"/);
  assert.match(chatSource, /模板示例/);
});

test("provides accessible, touch-sized clarification controls", () => {
  assert.match(chatSource, /aria-busy=\{disabled\}/);
  assert.match(chatSource, /aria-invalid=\{Boolean\(validationError\)\}/);
  assert.match(chatSource, /role="alert"/);
  assert.match(styleSource, /\.clarification-option \{ min-height: 44px/);
  assert.match(styleSource, /\.clarification-input-row input \{ min-width: 0; min-height: 44px/);
});
