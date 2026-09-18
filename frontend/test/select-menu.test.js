import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const selectSource = await readFile(new URL("../src/components/ui/SelectMenu.jsx", import.meta.url), "utf8");
const chatSource = await readFile(new URL("../src/components/chat/ChatWorkspace.jsx", import.meta.url), "utf8");
const profileSource = await readFile(new URL("../src/components/profile/ProfileMemoryManager.jsx", import.meta.url), "utf8");
const styleSource = await readFile(new URL("../src/styles-next.css", import.meta.url), "utf8");

test("uses one accessible SelectMenu implementation for chat and personalization controls", () => {
  assert.match(selectSource, /role="combobox"/);
  assert.match(selectSource, /role="listbox"/);
  assert.match(selectSource, /role="option"/);
  assert.match(selectSource, /aria-expanded=\{open\}/);
  assert.match(selectSource, /event\.key === "ArrowDown"/);
  assert.match(selectSource, /event\.key === "ArrowUp"/);
  assert.match(selectSource, /event\.key === "Home"/);
  assert.match(selectSource, /event\.key === "End"/);
  assert.match(selectSource, /event\.key === "Enter"/);
  assert.match(selectSource, /event\.key === "Escape"/);
  assert.match(selectSource, /document\.addEventListener\("pointerdown"/);
  assert.match(selectSource, /disabled=\{disabled\}/);
  assert.match(styleSource, /\.select-menu-trigger \{[\s\S]*min-height: 44px/);
  assert.match(styleSource, /\.select-menu-option \{[\s\S]*min-height: 44px/);
  assert.match(chatSource, /import \{ SelectMenu \} from "\.\.\/ui\/SelectMenu"/);
  assert.match(profileSource, /import \{ SelectMenu \} from "\.\.\/ui\/SelectMenu"/);
  assert.doesNotMatch(chatSource, /<select[\s>]/);
  assert.doesNotMatch(profileSource, /<select[\s>]/);
});

test("new conversation home keeps exactly Linux, Git and Python examples without hero copy", () => {
  const emptyState = chatSource.match(/function EmptyState[\s\S]*?\n}\n\nfunction Sidebar/)?.[0] || "";
  const examples = chatSource.match(/const examples = \[[\s\S]*?\];/)?.[0] || "";
  assert.match(emptyState, /03 ENTRIES/);
  assert.equal((emptyState.match(/<button key=\{id\}/g) || []).length, 1);
  assert.deepEqual([...examples.matchAll(/id: "([^"]+)"/g)].map((match) => match[1]), ["linux", "git", "python"]);
  assert.doesNotMatch(emptyState, /LOCAL-FIRST COMMAND RAG|把问题变成|CURATED LOCALLY|TRACEABLE SOURCES|SAFE BY DEFAULT/);
});
