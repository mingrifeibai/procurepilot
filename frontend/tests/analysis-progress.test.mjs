import assert from "node:assert/strict";
import { test } from "node:test";
import { readAnalysisProgress } from "../src/AnalysisProgress.ts";

const cursor = "a".repeat(64);
const latest = "b".repeat(64);

function eventStream(...parts) {
  const encode = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const part of parts) controller.enqueue(encode.encode(part));
        controller.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}

test("processes split frames and carries the last cursor across requests", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, "/api/w/fixture/analysis-events");
    assert.equal(options.headers.Authorization, "Bearer fixture-token");
    calls++;
    if (calls === 1) {
      assert.equal(options.headers["Last-Event-ID"], "");
      return eventStream(": heartbeat\n\n", `id: ${cursor}\nevent: ana`, "lysis_changed\ndata: {}\n\n");
    }
    assert.equal(options.headers["Last-Event-ID"], cursor);
    return eventStream(`id: ${latest}\nevent: analysis_changed\ndata: {}\n\n`);
  });
  const signal = new AbortController().signal;
  const seen = [];
  let connected = 0;
  const connect = (id) => readAnalysisProgress(
    "fixture", "fixture-token", id, signal, () => connected++, (next) => seen.push(next),
  );
  await connect("");
  await connect(seen.at(-1));
  assert.deepEqual(seen, [cursor, latest]);
  assert.equal(connected, 2);
});

test("signals fallback on non-SSE responses", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("unauthorized", { status: 401 }));
  await assert.rejects(readAnalysisProgress(
    "fixture", "invalid", "", new AbortController().signal, assert.fail, assert.fail,
  ), /分析订阅不可用/);
});
