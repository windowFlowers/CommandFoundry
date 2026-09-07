import assert from "node:assert/strict";
import test from "node:test";

import { parseSseFrame } from "../src/lib/api.js";


test("parses named SSE events", () => {
  assert.deepEqual(parseSseFrame('event: answer\ndata: {"answer":{"mode":"local_fallback"}}'), {
    event: "answer",
    data: { answer: { mode: "local_fallback" } },
  });
});


test("joins multiline SSE data", () => {
  assert.deepEqual(parseSseFrame('event: status\ndata: {"message":\ndata: "检索中"}'), {
    event: "status",
    data: { message: "检索中" },
  });
});
