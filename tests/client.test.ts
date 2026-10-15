import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ModelClient } from "../src/client.js";
import { Events } from "../src/events.js";
test("SSE handles fragmented transport; stores final answer and usage only", async () => {
  const original = globalThis.fetch;
  const dir = mkdtempSync(join(tmpdir(), "ld-stream-"));
  let request: any;
  const frames = [
    {
      type: "message_start",
      message: { model: "fixture", usage: { input_tokens: 10 } },
    },
    {
      type: "content_block_delta",
      delta: { type: "thinking_delta", thinking: "private reasoning" },
    },
    {
      type: "content_block_delta",
      delta: { type: "text_delta", text: '{"summary":"ok"}' },
    },
    {
      type: "message_delta",
      usage: { output_tokens: 20 },
      delta: { stop_reason: "end_turn" },
    },
  ]
    .map((e) => "data: " + JSON.stringify(e) + "\n\n")
    .join("");
  globalThis.fetch = async (_url, init) => {
    request = JSON.parse(String(init!.body));
    const bytes = new TextEncoder().encode(frames);
    return new Response(
      new ReadableStream({
        start(c) {
          for (let i = 0; i < bytes.length; i += 7)
            c.enqueue(bytes.slice(i, i + 7));
          c.close();
        },
      }),
      { headers: { "content-type": "text/event-stream" } },
    );
  };
  try {
    const answer = await new ModelClient(
      "https://example.invalid",
      "test",
      "secret",
      "high",
      32768,
    ).generate(
      "system",
      {},
      5,
      new Events(join(dir, "events"), "test", performance.now()),
      new AbortController().signal,
    );
    assert.equal(answer.text, '{"summary":"ok"}');
    assert.equal(answer.usage.output_tokens, 20);
    assert.equal(JSON.stringify(answer).includes("private reasoning"), false);
    assert.equal(request.output_config.effort, "high");
    assert.equal(request.max_tokens, 32768);
  } finally {
    globalThis.fetch = original;
    rmSync(dir, { recursive: true, force: true });
  }
});
test("truncated model answer cannot become an executable plan; errors redact credentials", async () => {
  const original = globalThis.fetch;
  const dir = mkdtempSync(join(tmpdir(), "ld-stream-"));
  const e = new Events(join(dir, "events"), "test", performance.now());
  try {
    globalThis.fetch = async () =>
      new Response(
        JSON.stringify({
          content: [{ type: "text", text: "{}" }],
          stop_reason: "max_tokens",
        }),
        { headers: { "content-type": "application/json" } },
      );
    await assert.rejects(
      new ModelClient(
        "https://example.invalid",
        "test",
        "secret",
        "high",
        32768,
      ).generate("", {}, 5, e, new AbortController().signal),
      /incomplete/,
    );
    globalThis.fetch = async () => new Response("oops secret", { status: 401 });
    await assert.rejects(
      new ModelClient(
        "https://example.invalid",
        "test",
        "secret",
        "high",
        32768,
      ).generate("", {}, 5, e, new AbortController().signal),
      (e) => String(e).includes("[REDACTED]") && !String(e).includes("secret"),
    );
  } finally {
    globalThis.fetch = original;
    rmSync(dir, { recursive: true, force: true });
  }
});
