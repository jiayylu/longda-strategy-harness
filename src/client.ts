import type { Events } from "./events.js";
export class ModelClient {
  constructor(
    readonly base: string,
    readonly model: string,
    private token: string,
    readonly effort: string,
    readonly maxTokens: number,
  ) {}
  async generate(
    system: string,
    payload: unknown,
    seconds: number,
    events: Events,
    signal: AbortSignal,
  ) {
    const started = performance.now();
    let text = "",
      chars = 0,
      usage: Record<string, unknown> = {},
      stopReason: string | undefined,
      responseModel = "";
    const timer = setInterval(
      () =>
        events.send("model_activity", {
          wait_s: Math.round((performance.now() - started) / 1000),
          chars,
        }),
      30000,
    );
    const body = {
      model: this.model,
      max_tokens: this.maxTokens,
      stream: true,
      system,
      messages: [{ role: "user", content: JSON.stringify(payload) }],
      thinking: { type: this.effort === "off" ? "disabled" : "enabled" },
      ...(this.effort === "off"
        ? {}
        : { output_config: { effort: this.effort } }),
    };
    const safe = (s: string) => s.split(this.token).join("[REDACTED]");
    try {
      const r = await fetch(this.base.replace(/\/$/, "") + "/v1/messages", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "anthropic-version": "2023-06-01",
          Authorization: "Bearer " + this.token,
        },
        body: JSON.stringify(body),
        signal: AbortSignal.any([
          signal,
          AbortSignal.timeout(Math.max(1, Math.floor(seconds * 1000))),
        ]),
      });
      if (!r.ok)
        throw Error(
          `Model HTTP ${r.status}: ${safe((await r.text()).slice(0, 1500))}`,
        );
      if (!r.body) throw Error("No response stream");
      const consume = (event: Record<string, any>) => {
        if (event.type === "error") throw Error(safe(JSON.stringify(event)));
        if (event.type === "message_start") {
          usage = { ...usage, ...event.message?.usage };
          responseModel = event.message?.model ?? "";
        }
        if (
          event.type === "content_block_start" &&
          event.content_block?.type === "text"
        )
          text += event.content_block.text ?? "";
        if (event.type === "content_block_delta") {
          const d = event.delta ?? {};
          chars += String(d.text ?? d.thinking ?? "").length;
          if (d.type === "text_delta") text += d.text ?? "";
        }
        if (event.type === "message_delta") {
          usage = { ...usage, ...event.usage };
          stopReason = event.delta?.stop_reason ?? stopReason;
        }
      };
      if (!r.headers.get("content-type")?.includes("text/event-stream")) {
        const data = (await r.json()) as any;
        text = (data.content ?? [])
          .filter((b: any) => b.type === "text")
          .map((b: any) => b.text)
          .join("\n");
        usage = data.usage ?? {};
        stopReason = data.stop_reason;
        responseModel = data.model;
      } else {
        const decoder = new TextDecoder();
        let buffer = "";
        for await (const chunk of r.body as any) {
          buffer += decoder.decode(chunk, { stream: true });
          buffer = buffer.replace(/\r\n/g, "\n");
          let end;
          while ((end = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, end);
            buffer = buffer.slice(end + 2);
            const data = frame
              .split("\n")
              .filter((x) => x.startsWith("data:"))
              .map((x) => x.slice(5).trim())
              .join("\n");
            if (data && data !== "[DONE]") consume(JSON.parse(data));
          }
        }
        if (buffer.trim()) throw Error("Incomplete SSE frame");
      }
      if (stopReason !== "end_turn")
        throw Error(
          `Model answer incomplete (${stopReason ?? "missing stop reason"})`,
        );
      return {
        text: safe(text),
        usage,
        response_model: responseModel,
        stop_reason: stopReason,
        wall_s: (performance.now() - started) / 1000,
      };
    } catch (e) {
      throw Error(safe(String(e)));
    } finally {
      clearInterval(timer);
    }
  }
}
