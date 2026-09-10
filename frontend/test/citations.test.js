import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const appSource = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
const chatSource = await readFile(new URL("../src/components/chat/ChatWorkspace.jsx", import.meta.url), "utf8");
const knowledgeSource = await readFile(new URL("../src/components/knowledge/KnowledgeManager.jsx", import.meta.url), "utf8");

test("renders grounded segment and command citation ids with stable answer numbering", () => {
  assert.match(chatSource, /const citationMap = new Map/);
  assert.match(chatSource, /answer\.summary_segments \|\| \[\]/);
  assert.match(chatSource, /referencesForIds\(segment\.citation_ids, citationMap\)/);
  assert.match(chatSource, /referencesForIds\(command\.citation_ids, citationMap\)/);
  assert.match(chatSource, /const label = `\[\$\{reference\.number\}\]`/);
});

test("keeps answers without summary segments on the legacy summary path", () => {
  assert.match(chatSource, /segments\.length > 0[\s\S]*:\s*<p className="answer-summary">\{answer\.summary\}<\/p>/);
});

test("requests an uploaded citation by chunk and detects changed source versions", () => {
  assert.match(appSource, /\?chunk_id=\$\{encodeURIComponent\(citation\.chunk_id\)\}/);
  assert.match(appSource, /citation\.index_revision !== payload\.index_revision/);
  assert.match(appSource, /citation\.document_sha256 !== payload\.document_sha256/);
  assert.match(chatSource, /onPreviewDocument\(reference\.citation\)/);
});

test("highlights and scrolls to the precise document range while showing locators", () => {
  assert.match(knowledgeSource, /preview\?\.highlight_start \?\? preview\?\.locator\?\.char_start/);
  assert.match(knowledgeSource, /<mark ref=\{highlightRef\} data-ui="document-highlight">/);
  assert.match(knowledgeSource, /scrollIntoView\(\{ block: "center", behavior: "auto" \}\)/);
  assert.match(knowledgeSource, /locator\.heading_path\.join\(" \/ "\)/);
  assert.match(knowledgeSource, /locator\.page_start, locator\.page_end, "页"/);
  assert.match(knowledgeSource, /locator\.line_start, locator\.line_end, "行"/);
  assert.match(knowledgeSource, /locator\.paragraph_start, locator\.paragraph_end, "段"/);
  assert.match(knowledgeSource, /locator\.table_row_start, locator\.table_row_end, "行"/);
  assert.match(knowledgeSource, /引用版本已变化/);
});

test("uses the same compatible preview component in chat and knowledge management", () => {
  assert.match(appSource, /import \{ DocumentPreviewModal, KnowledgeManager \}/);
  assert.match(appSource, /<DocumentPreviewModal preview=\{preview\}/);
  assert.match(knowledgeSource, /<DocumentPreviewModal preview=\{preview\}/);
});
