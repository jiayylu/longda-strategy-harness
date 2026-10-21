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
      4096,
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
    assert.equal(request.output_config, undefined);
    assert.equal(request.max_tokens, 32768);
    assert.equal(request.thinking.budget_tokens, 4096);
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

test("finished SSE answer is returned without waiting for proxy socket closure", async () => {
  const original = globalThis.fetch;
  const dir = mkdtempSync(join(tmpdir(), "ld-finished-"));
  let cancelled = false;
  globalThis.fetch = async () =>
    new Response(
      new ReadableStream({
        start(c) {
          for (const x of [
            {
              type: "content_block_delta",
              delta: { type: "text_delta", text: '{"summary":"done"}' },
            },
            { type: "message_delta", delta: { stop_reason: "end_turn" } },
          ])
            c.enqueue(
              new TextEncoder().encode("data: " + JSON.stringify(x) + "\n\n"),
            );
          // Deliberately leave the HTTP stream open, as a proxy may do.
        },
        cancel() {
          cancelled = true;
        },
      }),
      { headers: { "content-type": "text/event-stream" } },
    );
  try {
    const answer = await new ModelClient(
      "https://example.invalid",
      "test",
      "secret",
      "high",
      32768,
    ).generate(
      "",
      {},
      0.2,
      new Events(join(dir, "events"), "test", performance.now()),
      new AbortController().signal,
    );
    assert.equal(answer.text, '{"summary":"done"}');
    assert.ok(cancelled);
  } finally {
    globalThis.fetch = original;
    rmSync(dir, { recursive: true, force: true });
  }
});

for (const scenario of [
  "thinking",
  "partial_answer",
  "user_stop",
  "delayed_success",
] as const) {
  test(`model deadline diagnostics: ${scenario}`, async () => {
    const original = globalThis.fetch;
    const dir = mkdtempSync(join(tmpdir(), "ld-deadline-"));
    const controller = new AbortController();
    let delayed: ReturnType<typeof setTimeout> | undefined;
    const snapshots: any[] = [];
    globalThis.fetch = async (_url, init) =>
      new Response(
        new ReadableStream({
          start(c) {
            const send = (x: any) =>
              c.enqueue(
                new TextEncoder().encode("data: " + JSON.stringify(x) + "\n\n"),
              );
            send({
              type: "content_block_delta",
              delta: {
                type: "thinking_delta",
                thinking: "private-thought-never-store",
              },
            });
            if (scenario === "partial_answer")
              send({
                type: "content_block_delta",
                delta: { type: "text_delta", text: '{"summary":"secret' },
              });
            init!.signal!.addEventListener(
              "abort",
              () => c.error(init!.signal!.reason),
              { once: true },
            );
            if (scenario === "user_stop")
              delayed = setTimeout(() => controller.abort(), 10);
            if (scenario === "delayed_success")
              delayed = setTimeout(() => {
                send({
                  type: "content_block_delta",
                  delta: { type: "text_delta", text: "{}" },
                });
                send({
                  type: "message_delta",
                  delta: { stop_reason: "end_turn" },
                });
              }, 70);
          },
        }),
        { headers: { "content-type": "text/event-stream" } },
      );
    const events = new Events(join(dir, "events"), "test", performance.now());
    try {
      const call = new ModelClient(
        "https://example.invalid",
        "test",
        "secret",
        "high",
        32768,
      ).generate(
        "",
        {},
        scenario === "delayed_success" ? 0.3 : 0.04,
        events,
        controller.signal,
        (d) => snapshots.push(d),
      );
      if (scenario === "delayed_success") {
        const x = await call;
        assert.equal(x.text, "{}");
      } else {
        await assert.rejects(call, (e: any) => {
          assert.equal(
            e.code,
            scenario === "user_stop" ? "cancelled" : "api_timeout",
          );
          assert.ok(e.diagnostics.thinking_chars > 0);
          assert.equal(
            JSON.stringify(e).includes("private-thought-never-store"),
            false,
          );
          if (scenario === "partial_answer") {
            assert.equal(e.diagnostics.stage, "answer");
            assert.ok(e.diagnostics.answer_text.includes("[REDACTED]"));
          }
          return true;
        });
      }
      assert.ok(snapshots.length >= 2);
      assert.equal(
        JSON.stringify(snapshots).includes("private-thought-never-store"),
        false,
      );
    } finally {
      if (delayed) clearTimeout(delayed);
      globalThis.fetch = original;
      rmSync(dir, { recursive: true, force: true });
    }
  });
}
